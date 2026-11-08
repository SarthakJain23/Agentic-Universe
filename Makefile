SCENARIO ?= scenarios/public_01_dev.yaml
NAME ?= $(basename $(notdir $(SCENARIO)))

baseline:
	python -m sim baseline --scenario $(SCENARIO) --run-dir runs/base_$(NAME)
serve:
	python -m sim serve --scenario $(SCENARIO) --run-dir runs/mine_$(NAME) --force
grade:
	mkdir -p reports && python -m sim grade --run runs/mine_$(NAME) --baseline runs/base_$(NAME) --out reports/$(NAME).md
test:
	python -m pytest -q tests

# ---- controller
PORT ?= 8700
scenario:          ## baseline (if missing) + serve + ./run.sh + grade -> reports/$(NAME).md
	scripts/run_scenario.sh $(SCENARIO) $(NAME) $(PORT)
crash:             ## kill -9 the controller at random points, restart, verify invariants
	scripts/crash_test.sh scenarios/public_02_incidents.yaml 5 8731
status:            ## loop health from a store: make status DB=state/public_02.db
	python -m controller status --db $(DB)
