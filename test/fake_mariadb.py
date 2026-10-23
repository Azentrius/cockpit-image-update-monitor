#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The MariaDB client and mariadb-dump for test/mariadb_dump_test.py: the
scripts of diun-update.py run with sh as in the container, and this answers
their queries from the accounts of a server in $FAKE_MARIADB (JSON), as
MariaDB does (QUOTE(), SHOW CREATE USER, SHOW GRANTS). An error on demand:
every query that contains one of the strings in "fail" ends with an error
line on stderr and exit 1, as the client does.

    fake_mariadb.py mariadb|mariadb-dump [options] [-e <sql>]"""
import json
import os
import re
import sys

STATE = json.load(open(os.environ["FAKE_MARIADB"]))
PROG, ARGS = sys.argv[1], sys.argv[2:]
SQL = ARGS[ARGS.index("-e") + 1] if "-e" in ARGS else ""


def quote(s):
    """QUOTE() of MariaDB: a string literal with \\ and ' escaped."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def fail(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(1)


def literal(text):
    """The accounts a SHOW names: 'u'@'h', 'role' or PUBLIC -> (user, host or None)."""
    text = text.strip().rstrip(";").strip()
    if text == "PUBLIC":
        return ("PUBLIC", "")
    parts, i = [], 0
    while i < len(text):
        if text[i] == "'":
            j, s = i + 1, ""
            while text[j] != "'":
                if text[j] == "\\":
                    j += 1
                s += text[j]
                j += 1
            parts.append(s)
            i = j + 1
        elif text[i] == "@":
            i += 1
        else:
            fail(f"ERROR 1064 (42000) at line 1: You have an error in your SQL syntax near '{text[i:]}'")
    return (parts[0], parts[1] if len(parts) > 1 else None)


def account(spec):
    user, host = literal(spec)
    for a in STATE["accounts"]:
        if a["user"] == user and (a.get("host", "") == host or (host is None and a.get("role"))):
            return a
    fail(f"ERROR 1141 (42000) at line 1: There is no such grant defined for user '{user}' on host '{host or '%'}'")


for bad in STATE.get("fail", []):
    if bad in SQL:
        fail(f"ERROR 1142 (42000) at line 1: stand-in failure of: {SQL[:80]}")

if PROG == "mariadb-dump":
    names = ARGS[ARGS.index("--databases") + 1:] if "--databases" in ARGS else []
    for n in names:
        print(f"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `{n}`;")
    sys.exit(0)

low = SQL.lower()
if "information_schema.schemata" in low:
    for d in STATE.get("dbs", []):
        print(d)
elif low.startswith("show create user "):
    print(account(SQL[len("show create user "):])["create"])
elif low.startswith("show grants for "):
    for line in account(SQL[len("show grants for "):])["grants"]:
        print(line)
elif "from mysql.user" in low:
    rows = STATE["accounts"]
    where = low.split(" from mysql.user", 1)[1]          # the filters, not the columns
    m = re.search(r"user not in \(([^)]*)\)", where)
    if m:
        out = {s.strip().strip("'") for s in m.group(1).split(",")}
        rows = [a for a in rows if a["user"] not in out]
    if "is_role = 'n'" in where:
        rows = [a for a in rows if not a.get("role")]
    if "is_role = 'y'" in where:
        rows = [a for a in rows if a.get("role")]
    if "user <> 'public'" in where:
        rows = [a for a in rows if a["user"] != "PUBLIC"]
    if "user = 'public'" in where:
        rows = [a for a in rows if a["user"] == "PUBLIC"]
    if "user = 'image_updates_import_role'" in where:
        rows = [a for a in rows if a["user"] == "image_updates_import_role"]
    if "default_role <> ''" in where:
        rows = [a for a in rows if a.get("default")]
    rows = sorted(rows, key=lambda a: (bool(a.get("role")), a["user"], a.get("host", "")))
    if low.startswith("select count(*)"):
        print(len(rows))
    elif "set default role" in low:
        for a in rows:
            print(f"SET DEFAULT ROLE {quote(a['default'])} FOR {quote(a['user'])}@{quote(a['host'])};")
    elif low.startswith("select if(is_role"):
        for a in rows:
            print(("PUBLIC" if a["user"] == "PUBLIC" else quote(a["user"])) if a.get("role")
                  else f"{quote(a['user'])}@{quote(a['host'])}")
    elif low.startswith("select concat(quote(user), '@', quote(host))"):
        for a in rows:
            print(f"{quote(a['user'])}@{quote(a['host'])}")
    elif low.startswith("select quote(user)"):
        for a in rows:
            print(quote(a["user"]))
    else:
        fail(f"ERROR 1064 (42000): the stand-in does not know: {SQL[:120]}")
else:
    fail(f"ERROR 1064 (42000): the stand-in does not know: {SQL[:120]}")
