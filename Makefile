
# Makefile to automate the build process for the Steam runtime

ifeq "$(ARCHIVE_OUTPUT_DIR)" ""
	ARCHIVE_OUTPUT_DIR := /tmp/steam-runtime
endif
ifeq "$(ARCHIVE_VERSION_TAG)" ""
	ARCHIVE_VERSION_TAG := $(shell date +%F)
endif
ARCHIVE_CUSTOMER_RUNTIME := $(ARCHIVE_OUTPUT_DIR)/steam-runtime-bin-$(ARCHIVE_VERSION_TAG).tar.bz2
ARCHIVE_DEVELOPER_RUNTIME := $(ARCHIVE_OUTPUT_DIR)/steam-runtime-dev-$(ARCHIVE_VERSION_TAG).tar.bz2
ARCHIVE_COMPLETE_RUNTIME := $(ARCHIVE_OUTPUT_DIR)/steam-runtime-src-$(ARCHIVE_VERSION_TAG).tar.bz2

all: clean-log amd64 i386

amd64 i386:
	./buildroot.sh --arch=$@ ./build-runtime.sh --runtime="$(RUNTIME_PATH)" --devmode="$(DEVELOPER_MODE)" | tee -a build.log

update:
	./update-packages.sh

clean-log:
	@rm -f build.log

clean-runtime:
	@./clean-runtime.sh

clean-buildroot:
	@./buildroot.sh --archive --clean

clean: clean-log clean-runtime clean-buildroot

archive: archive-customer-runtime archive-developer-runtime archive-complete-runtime
	@ls -l "$(ARCHIVE_OUTPUT_DIR)"

archive-customer-runtime:
	@if [ -d tmp ]; then chmod u+w -R tmp; rm -rf tmp; fi
	make clean-runtime
	mkdir -p tmp/steam-runtime
	cp -a runtime/* tmp/steam-runtime
	make RUNTIME_PATH="$(CURDIR)/tmp/steam-runtime" DEVELOPER_MODE=false || exit 1
	@echo ""
	@echo "Creating $(ARCHIVE_CUSTOMER_RUNTIME)"
	mkdir -p "$(ARCHIVE_OUTPUT_DIR)"
	(cd tmp; tar acf "$(ARCHIVE_CUSTOMER_RUNTIME)" steam-runtime) || exit 2
	@if [ -d tmp ]; then chmod u+w -R tmp; rm -rf tmp; fi

archive-developer-runtime:
	@if [ -d tmp ]; then chmod u+w -R tmp; rm -rf tmp; fi
	make clean-runtime
	mkdir -p tmp/steam-runtime
	cp -a x-tools/* runtime tmp/steam-runtime
	make RUNTIME_PATH="$(CURDIR)/tmp/steam-runtime/runtime" DEVELOPER_MODE=true || exit 1
	@echo ""
	@echo "Creating $(ARCHIVE_DEVELOPER_RUNTIME)"
	mkdir -p "$(ARCHIVE_OUTPUT_DIR)"
	(cd tmp; tar acf "$(ARCHIVE_DEVELOPER_RUNTIME)" steam-runtime) || exit 2
	@if [ -d tmp ]; then chmod u+w -R tmp; rm -rf tmp; fi

archive-complete-runtime:
	@if [ -d tmp ]; then chmod u+w -R tmp; rm -rf tmp; fi
	make clean
	@echo ""
	@echo "Creating $(ARCHIVE_COMPLETE_RUNTIME)"
	mkdir -p "$(ARCHIVE_OUTPUT_DIR)"
	(cd tmp; tar acf "$(ARCHIVE_COMPLETE_RUNTIME)" steam-runtime) || exit 2

distclean: clean
	@rm -rf packages
	@rm -rf buildroot/pbuilder
