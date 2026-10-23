#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""What a MariaDB major takes along besides the databases: the users and
roles with their grants (MY_MAJOR_DUMP of diun-update.py), and the list of
every account's grants it compares before and after (MY_GRANTS). Both run
with sh as in the container, the client stood in for by test/fake_mariadb.py
(the accounts of a server answered as MariaDB does, errors on demand).

The SQL the dump writes is then played on a small model of MariaDB's
accounts (Restore below): a statement that names a user or role before it
exists, grants a role the restoring user does not reach with the admin
option, or needs privileges the session no longer has (SET ROLE computes
them anew from root's account, which the dump replaces), fails there as it
does on a server. What the model holds at the end must be what the server had:
every user and role, their privileges, the roles granted to users and roles
(with the admin option), the default roles, roles nobody has, and nothing
more (the restore's own role is gone again). An error while reading an
account, its grants or the lists ends the dump with an error and without the
end marker. The real thing, MariaDB 11 to 12, is section 17d of
test/e2e-server.sh."""
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(os.path.dirname(HERE), "lib")
sys.path.insert(0, ROOT)
spec = importlib.util.spec_from_file_location("diun_update_mydump", os.path.join(ROOT, "diun-update.py"))
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)

TMP = tempfile.mkdtemp(prefix="mariadb-dump-")
BIN = os.path.join(TMP, "bin")
os.mkdir(BIN)
for prog in ("mariadb", "mariadb-dump"):
    path = os.path.join(BIN, prog)
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\nexec "{sys.executable}" "{os.path.join(HERE, "fake_mariadb.py")}" {prog} "$@"\n')
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("OK   " if ok else "FAIL ") + label + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


def user(name, host, grants, default="", password="*0123456789ABCDEF"):
    spec = f"`{name}`@`{host}`"
    return {"user": name, "host": host, "default": default,
            "create": f"CREATE USER {spec} IDENTIFIED BY PASSWORD '{password}'",
            "grants": [f"GRANT USAGE ON *.* TO {spec} IDENTIFIED BY PASSWORD '{password}'"] + grants
            + ([f"SET DEFAULT ROLE `{default}` FOR {spec}"] if default else [])}


def role(name, grants):
    return {"user": name, "host": "", "role": True, "grants": [f"GRANT USAGE ON *.* TO `{name}`"] + grants}


# A server with the cases of the review: a user with rights only through a
# role (staff) and that role as default, a role that has another (editor has
# viewer), a role nobody has with rights of its own (auditor), a user with
# direct rights (app), a role granted with the admin option (lead), roles the
# root user created (it holds them with the admin option, as CREATE ROLE
# leaves it), a name with a quote in it, a role in PUBLIC, and the accounts
# the image keeps for itself.
SERVER = {"dbs": ["appdb", "shop"], "accounts": [
    {"user": "root", "host": "localhost", "default": "",
     "create": "CREATE USER `root`@`localhost` IDENTIFIED VIA mysql_native_password USING '*ROOT' OR unix_socket",
     "grants": ["GRANT ALL PRIVILEGES ON *.* TO `root`@`localhost` IDENTIFIED VIA mysql_native_password USING '*ROOT' "
                "OR unix_socket WITH GRANT OPTION", "GRANT PROXY ON ''@'%' TO 'root'@'localhost' WITH GRANT OPTION",
                "GRANT `editor` TO `root`@`localhost` WITH ADMIN OPTION",
                "GRANT `viewer` TO `root`@`localhost` WITH ADMIN OPTION"]},
    user("root", "%", ["GRANT ALL PRIVILEGES ON *.* TO `root`@`%` WITH GRANT OPTION"], password="*ROOT"),
    user("app", "%", ["GRANT ALL PRIVILEGES ON `appdb`.* TO `app`@`%`"]),
    user("staff", "%", ["GRANT `editor` TO `staff`@`%`"], default="editor"),
    user("lead", "%", ["GRANT SELECT ON `shop`.* TO `lead`@`%`", "GRANT `viewer` TO `lead`@`%` WITH ADMIN OPTION"]),
    user("O'Brien", "10.0.0.%", ["GRANT SELECT ON `shop`.`items` TO `O'Brien`@`10.0.0.%`"]),
    user("mariadb.sys", "localhost", ["GRANT SELECT, DELETE ON `mysql`.`global_priv` TO `mariadb.sys`@`localhost`"]),
    user("healthcheck", "localhost", ["GRANT USAGE ON *.* TO `healthcheck`@`localhost`"]),
    role("viewer", ["GRANT SELECT ON `appdb`.`notes` TO `viewer`"]),
    role("editor", ["GRANT INSERT, UPDATE ON `appdb`.* TO `editor`", "GRANT `viewer` TO `editor`"]),
    role("auditor", ["GRANT SELECT ON `shop`.* TO `auditor`"]),
    {"user": "PUBLIC", "host": "", "role": True, "grants": ["GRANT SELECT ON `shop`.`catalogue` TO PUBLIC"]}]}
LEFT_OUT = {("mariadb.sys", "localhost"), ("healthcheck", "localhost")}


def sh(script, server):
    path = os.path.join(TMP, "server.json")
    with open(path, "w") as f:
        json.dump(server, f)
    env = dict(os.environ, PATH=BIN + os.pathsep + os.environ.get("PATH", ""), FAKE_MARIADB=path,
               MARIADB_ROOT_PASSWORD="the-root-secret")
    p = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    return p.returncode, p.stdout, p.stderr


# ------------------------------------------------------- the model ----
GRANTEE = r"(`(?:[^`]|``)*`(?:@`(?:[^`]|``)*`)?|'(?:[^'\\]|\\.)*'(?:@'(?:[^'\\]|\\.)*')?|PUBLIC)"


def key_of(text):
    """`u`@`h`, 'u'@'h', `r`, 'r' or PUBLIC -> (user, host); a role's host is ""."""
    if text == "PUBLIC":
        return ("PUBLIC", "")
    parts = re.findall(r"`((?:[^`]|``)*)`|'((?:[^'\\]|\\.)*)'", text)
    vals = [(a.replace("``", "`") if a or not b else re.sub(r"\\(.)", r"\1", b)) for a, b in parts]
    return (vals[0], vals[1] if len(vals) > 1 else "")


def spec_of(k):
    u, h = k
    return "PUBLIC" if k == ("PUBLIC", "") else (f"`{u}`@`{h}`" if h else f"`{u}`")


class Restore:
    """MariaDB's accounts as far as the dump touches them, restored as root@localhost."""

    def __init__(self):
        # The new server: root, and PUBLIC, which every server has (it cannot be created).
        self.acc = {("root", "localhost"): {"role": False, "privs": [], "roles": {}, "default": ""},
                    ("PUBLIC", ""): {"role": True, "privs": [], "roles": {}, "default": ""}}
        # The session's privileges: root's ALL, as computed at login; SET ROLE computes them anew
        # from root's account as it is then.
        self.session_all = True

    def privileged(self, what):
        if not self.session_all:
            raise ValueError(f"access denied for {what}: the session's privileges were computed anew "
                             f"(SET ROLE) from root@localhost, which has none yet")

    def need(self, k, what):
        if k not in self.acc:
            raise ValueError(f"{what} {spec_of(k)} does not exist")
        return self.acc[k]

    def admin_of(self, r):
        """Can the restoring user grant role r: as MariaDB's can_grant_role, some
        role granted to root@localhost, directly or through other roles, holds
        it with the admin option (whatever role is active)."""
        todo, seen = [("root", "localhost")], set()
        while todo:
            k = todo.pop()
            if k in seen or k not in self.acc:
                continue
            seen.add(k)
            for name, admin in self.acc[k]["roles"].items():
                if name == r and admin:
                    return True
                todo.append((name, ""))
        return False

    def play(self, stmt):
        s = stmt.strip()
        if not s or s.startswith("--"):
            return
        assert s.endswith(";"), s
        s = s[:-1]
        m = re.match(r"CREATE OR REPLACE USER (" + GRANTEE + r") ", s)
        if m:
            self.privileged("CREATE USER")
            self.acc[key_of(m.group(1))] = {"role": False, "privs": [], "roles": {}, "default": ""}
            return
        m = re.match(r"CREATE (OR REPLACE )?ROLE (\S+)( WITH ADMIN (\S+))?$", s)
        if m:
            k = (key_of(m.group(2))[0] if m.group(2)[0] in "'`" else m.group(2), "")
            if k[0] == "PUBLIC":
                raise ValueError("CREATE ROLE PUBLIC")
            self.privileged("CREATE ROLE")
            if k in self.acc and not m.group(1):
                raise ValueError(f"role {k[0]} exists")
            self.acc[k] = {"role": True, "privs": [], "roles": {}, "default": ""}
            admin = (m.group(4), "") if m.group(4) else ("root", "localhost")      # WITH ADMIN CURRENT_USER
            self.need(admin, "admin").setdefault("roles", {})[k[0]] = True
            return
        m = re.match(r"SET ROLE (\S+)$", s)
        if m:
            r = m.group(1)
            if r != "NONE" and r not in self.acc[("root", "localhost")]["roles"]:
                raise ValueError(f"role {r} not granted to root@localhost")
            self.session_all = any(p.startswith("GRANT ALL PRIVILEGES ON *.* TO")
                                   for p in self.acc[("root", "localhost")]["privs"])
            return
        m = re.match(r"GRANT `((?:[^`]|``)*)` TO " + GRANTEE + r"( WITH ADMIN OPTION)?$", s)
        if m:
            r = m.group(1).replace("``", "`")
            self.need((r, ""), "role")
            to = self.need(key_of(m.group(2)), "grantee")
            if not self.admin_of(r):
                raise ValueError(f"no admin option for role {r}")
            to["roles"][r] = bool(m.group(3)) or to["roles"].get(r, False)
            return
        m = re.match(r"GRANT .+ ON .+ TO " + GRANTEE + r"(?: IDENTIFIED .*?)?(?: WITH GRANT OPTION)?$", s)
        if m:
            self.privileged("GRANT")
            self.need(key_of(m.group(1)), "grantee")["privs"].append(s)
            return
        m = re.match(r"SET DEFAULT ROLE (\S+) FOR (" + GRANTEE + r")$", s)
        if m:
            r, a = key_of(m.group(1))[0], self.need(key_of(m.group(2)), "user")
            self.privileged("SET DEFAULT ROLE FOR another user")
            if r not in a["roles"]:
                raise ValueError(f"default role {r} not granted to {m.group(2)}")
            a["default"] = r
            return
        m = re.match(r"DROP ROLE (\S+)$", s)
        if m:
            r = m.group(1)
            self.privileged("DROP ROLE")
            self.need((r, ""), "role")
            del self.acc[(r, "")]
            for a in self.acc.values():
                a["roles"].pop(r, None)
            return
        if s == "FLUSH PRIVILEGES":
            return
        raise ValueError(f"the model does not know: {s}")

    def grants(self):
        """{(user, host): sorted SHOW GRANTS lines} as the server would show them."""
        out = {}
        for k, a in self.acc.items():
            if k == ("PUBLIC", "") and not (a["privs"] or a["roles"]):
                continue                    # SHOW GRANTS lists PUBLIC once it has something
            lines = list(a["privs"]) + [f"GRANT `{r}` TO {spec_of(k)}" + (" WITH ADMIN OPTION" if adm else "")
                                        for r, adm in a["roles"].items()]
            if a["default"]:
                lines.append(f"SET DEFAULT ROLE `{a['default']}` FOR {spec_of(k)}")
            out[k] = sorted(lines)
        return out


def restored(sql):
    """Play the account part of a dump: (the model's grants, None) or (None, the first error)."""
    model = Restore()
    part = sql.split("-- Users and roles", 1)
    if len(part) < 2:
        part = sql.split("-- Users and their grants", 1)       # the dump of 0.25.0
    try:
        for stmt in part[1].splitlines():
            model.play(stmt)
    except (ValueError, IndexError) as e:
        return None, str(e)
    return model.grants(), None


def server_grants(server):
    return {(a["user"], a.get("host", "")): sorted(a["grants"]) for a in server["accounts"]
            if (a["user"], a.get("host", "")) not in LEFT_OUT}


# -------------------------------------------------------- the dump ----
rc, out, err = sh(U.MY_MAJOR_DUMP, SERVER)
check("dump: exit 0, the databases first, the end marker last", (rc, out.startswith("CREATE DATABASE"),
                                                                out.rstrip().endswith("-- Dump completed")), (0, True, True))
check("dump: the root password only in the environment (MYSQL_PWD), never on a command line or in the dump",
      "the-root-secret" in out, False)
got, error = restored(out)
check("dump played on the model: no statement names a user or role before it exists, every role is granted with "
      "the admin option for it", error, None)
want = server_grants(SERVER)
for k in sorted(want):
    check(f"dump played on the model: {spec_of(k)} has what it had on the server", (got or {}).get(k), want[k])
check("dump played on the model: nothing more than the server had (the restore's own role gone, root without the "
      "roles it did not hold)", sorted(set(got or {}) - set(want)), [])
roles_created = re.findall(r"^CREATE OR REPLACE ROLE (\S+) ", out, re.M)
check("dump: every role created, the one nobody has too, PUBLIC not (it cannot be created)",
      sorted(roles_created), ["'auditor'", "'editor'", "'viewer'"])
check("dump: the default roles once, after the grants, not the lines of SHOW GRANTS",
      [ln for ln in out.splitlines() if ln.startswith("SET DEFAULT ROLE")], ["SET DEFAULT ROLE 'editor' FOR 'staff'@'%';"])
check("dump: a name with a quote in it reaches the server as one name",
      "CREATE OR REPLACE USER `O'Brien`@`10.0.0.%` IDENTIFIED BY PASSWORD '*0123456789ABCDEF';" in out, True)
check("dump: the image's own accounts (healthcheck, mariadb.sys) stay out",
      ("healthcheck" in out, "mariadb.sys" in out), (False, False))
check("dump: no SET ROLE (it would take root's privileges away while its account is replaced)",
      [ln for ln in out.splitlines() if ln.startswith("SET ROLE")], [])
model = Restore()
for stmt in ["CREATE OR REPLACE USER `root`@`localhost` IDENTIFIED BY PASSWORD '*R';", "CREATE ROLE imp;",
             "SET ROLE imp;"]:
    model.play(stmt)
try:
    model.play("CREATE OR REPLACE ROLE 'r' WITH ADMIN imp;")
    denied = None
except ValueError as e:
    denied = str(e)
check("the model: after SET ROLE the replaced root has no privileges, CREATE ROLE is denied (as MariaDB 12 did in "
      "the end-to-end test)", (denied or "").startswith("access denied for CREATE ROLE"), True)

# Without roles: the users as before, the restore's own role made and dropped again.
plain = {"dbs": ["appdb"], "accounts": [a for a in SERVER["accounts"] if a["user"] in ("root", "app")]}
plain["accounts"][0] = dict(plain["accounts"][0], grants=plain["accounts"][0]["grants"][:2])
rc, out, _ = sh(U.MY_MAJOR_DUMP, plain)
got, error = restored(out)
check("dump without roles (users with direct rights only): exit 0, played on the model as the server had it",
      (rc, error, got), (0, None, server_grants(plain)))

# ----------------------------------------------- errors while reading ----
for label, bad in (("the list of databases", "information_schema.schemata"),
                   ("the list of users", "concat(quote(user), '@', quote(host)) from mysql.user where is_role = 'N'"),
                   ("the list of roles", "select quote(user) from mysql.user"),
                   ("the default roles", "SET DEFAULT ROLE"),
                   ("a user (SHOW CREATE USER)", "show create user 'staff'@'%'"),
                   ("the grants of a user", "show grants for 'staff'@'%'"),
                   ("the grants of a role", "show grants for 'editor'"),
                   ("the grants of PUBLIC", "show grants for PUBLIC")):
    rc, out, err = sh(U.MY_MAJOR_DUMP, dict(SERVER, fail=[bad]))
    check(f"dump: an error reading {label} ends the dump with an error, without the end marker",
          (rc != 0, "-- Dump completed" in out, "stand-in failure" in err), (True, False, True))

# ------------------------------------- the grants compared before and after ----
if not hasattr(U, "MY_GRANTS"):
    check("grants list (MY_GRANTS, account_grants, compare_grants) exists", False, True)
    print(f"\n{fails} FAILED")
    sys.exit(1)
rc, out, err = sh(U.MY_GRANTS, SERVER)
lines = [ln for ln in out.splitlines() if ln.strip()]
check("grants list: exit 0, one line per grant with its account in front, the image's own accounts left out",
      (rc, len(lines), sorted({ln.split("\t")[0] for ln in lines})),
      (0, sum(len(v) for v in want.values()),
       sorted(["'root'@'localhost'", "'root'@'%'", "'app'@'%'", "'staff'@'%'", "'lead'@'%'", "'O\\'Brien'@'10.0.0.%'",
               "'viewer'", "'editor'", "'auditor'", "PUBLIC"])))
rc, out, err = sh(U.MY_GRANTS, dict(SERVER, fail=["show grants for 'lead'@'%'"]))
check("grants list: an error reading the grants of an account ends it with an error", rc != 0, True)
U.pg_lines = lambda name, script, timeout=None: [ln for ln in sh(script, SERVER)[1].splitlines() if ln.strip()]
before = U.account_grants("db")
check("account_grants: {account: sorted lines}", (len(before), before["'staff'@'%'"]),
      (10, sorted(["GRANT USAGE ON *.* TO `staff`@`%` IDENTIFIED BY PASSWORD '*0123456789ABCDEF'",
                   "GRANT `editor` TO `staff`@`%`", "SET DEFAULT ROLE `editor` FOR `staff`@`%`"])))
after = json.loads(json.dumps(before))
check("compare_grants: the same", U.compare_grants(before, after), [])
after["'auditor'"] = None
del after["'auditor'"]
after["'staff'@'%'"] = [ln for ln in after["'staff'@'%'"] if not ln.startswith("SET DEFAULT")]
after["'root'@'localhost'"] = sorted(after["'root'@'localhost'"] + ["GRANT `auditor` TO `root`@`localhost` WITH ADMIN OPTION"])
after["'new'@'%'"] = ["GRANT USAGE ON *.* TO `new`@`%`"]
check("compare_grants: a role gone, a default role lost, a role more for root; an account the new image made "
      "itself does not count", U.compare_grants(before, after),
      ["'auditor': missing", "'root'@'localhost': has GRANT `auditor` TO `root`@`localhost` WITH ADMIN OPTION too",
       "'staff'@'%': lacks SET DEFAULT ROLE `editor` FOR `staff`@`%`"])

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
