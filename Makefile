PYTHON ?= python3
SOURCE ?= /home/xiazhangtao/code/X-RAN-CLOUD
XCN_SOURCE ?= $(or $(OPEN5GS_SOURCE),$(wildcard $(CURDIR)/../xcn),$(wildcard $(CURDIR)/../ICT_CN),$(wildcard $(CURDIR)/../open5gs),$(CURDIR)/../xcn)
SITE ?= config/site.yaml
IMAGE_NAME ?= 192.168.10.35/xnet/xran-compile
IMAGE_TAG ?= latest
DOCKERFILE ?= docker/builder-compat.Dockerfile
BUILD_JOBS ?= 4
APT_SOURCES ?= config/sources.online.list
OFFLINE ?= 0
shell_quote = '$(subst ','"'"',$(1))'
COMPILE_FLAGS = --image-name $(call shell_quote,$(IMAGE_NAME)) --image-tag $(call shell_quote,$(IMAGE_TAG)) --dockerfile $(call shell_quote,$(DOCKERFILE)) --jobs $(call shell_quote,$(BUILD_JOBS)) --apt-sources $(call shell_quote,$(APT_SOURCES)) $(if $(filter 1,$(OFFLINE)),--offline)
.PHONY: prepare images render import-images deploy bundle test verify backup compile-image push-compile-image platform-plugin
platform-plugin:
	$(PYTHON) scripts/platform-plugin.py $(if $(filter 1,$(OFFLINE)),--offline)
compile-image:
	$(PYTHON) scripts/compile-image.py build $(COMPILE_FLAGS)
push-compile-image:
	$(PYTHON) scripts/compile-image.py push --image-name $(call shell_quote,$(IMAGE_NAME)) --image-tag $(call shell_quote,$(IMAGE_TAG))
prepare images render import-images deploy bundle backup:
	$(PYTHON) scripts/ci.py $@ --site $(call shell_quote,$(SITE)) --source $(call shell_quote,$(SOURCE)) --xcn-source $(call shell_quote,$(XCN_SOURCE)) $(COMPILE_FLAGS)
test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
verify:
	$(PYTHON) tests/verify.py --site $(SITE) --source $(SOURCE)
