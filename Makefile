PYTHON ?= python3
SOURCE ?= /home/xiazhangtao/code/X-RAN-CLOUD
SITE ?= config/site.yaml
.PHONY: prepare images render import-images deploy bundle test verify backup
prepare images render import-images deploy bundle backup:
	$(PYTHON) scripts/ci.py $@ --site $(SITE) --source $(SOURCE)
test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
verify:
	$(PYTHON) tests/verify.py --site $(SITE) --source $(SOURCE)
