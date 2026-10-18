# Image Update Monitor, a Cockpit add-on for Docker image updates
# SPDX-License-Identifier: Apache-2.0
#
# The paths are fixed (the page, the helpers and the units refer to each other by them);
# DESTDIR only stages. The usual way to install is the package (make packages).

DESTDIR ?=
PAGE = $(DESTDIR)/usr/share/cockpit/image-updates
HELPERS = $(DESTDIR)/usr/lib/cockpit-image-updates
UNITS = $(DESTDIR)/usr/lib/systemd/system
VERSION := $(shell sed -n 's/^ *const VERSION = "\(.*\)";.*/\1/p' plugin/diun.js)
# Reproducible archives and packages: file times from the last commit.
SOURCE_DATE_EPOCH ?= $(shell git log -1 --format=%ct 2>/dev/null || date +%s)

.PHONY: all install uninstall check lint dist stage packages

all:
	@echo "Nothing to build. Try: make check, make lint, make packages, sudo make install"

# Also takes over an installation made by install.sh (up to 0.11), like the package does.
install:
	install -d $(PAGE) $(HELPERS) $(UNITS)
	install -m 644 plugin/* $(PAGE)/
	install -m 755 lib/*.py lib/*.sh $(HELPERS)/
	install -m 644 lib/diunlib.py lib/diunproc.py $(HELPERS)/
	install -m 644 systemd/*.service systemd/*.timer $(UNITS)/
	if [ -z "$(DESTDIR)" ]; then sh packaging/scripts/configure; fi

uninstall:
	sh packaging/scripts/remove
	rm -rf $(PAGE) $(HELPERS)
	rm -f $(UNITS)/diun-radar.service $(UNITS)/diun-radar.timer \
		$(UNITS)/diun-cleanup.service $(UNITS)/diun-cleanup.timer
	sh packaging/scripts/removed

# Every offline test (Python helpers, the page under jsdom, version and changelog).
check:
	cd test && npm ci && bash run-all.sh

# As in CI (the tools: pip install -r test/requirements.txt).
lint:
	shellcheck --severity=warning lib/*.sh test/*.sh tools/*.sh packaging/scripts/*
	ruff check .
	sh tools/check-units.sh

# Source archive of the last commit.
dist:
	git archive --format=tar.gz --prefix=cockpit-image-update-monitor-$(VERSION)/ \
		-o cockpit-image-update-monitor-$(VERSION).tar.gz HEAD
	@echo cockpit-image-update-monitor-$(VERSION).tar.gz

# The files of the .deb and .rpm, as installed, in build/root.
stage:
	rm -rf build/root
	$(MAKE) install DESTDIR=build/root
	install -D -m 644 packaging/de.azentrius.image_updates.metainfo.xml \
		build/root/usr/share/metainfo/de.azentrius.image_updates.metainfo.xml
	install -D -m 644 NOTICE build/root/usr/share/doc/cockpit-image-update-monitor/copyright
	install -m 644 LICENSE build/root/usr/share/doc/cockpit-image-update-monitor/LICENSE
	install -m 644 plugin/CC-BY-4.0-Font-Awesome.txt build/root/usr/share/doc/cockpit-image-update-monitor/

# cockpit-image-update-monitor_VERSION-1_all.deb and cockpit-image-update-monitor-VERSION-1.noarch.rpm in
# dist/ (needs dpkg-deb and rpmbuild), reproducible.
packages: stage
	SOURCE_DATE_EPOCH=$(SOURCE_DATE_EPOCH) sh tools/build-packages.sh build/root $(VERSION) dist
