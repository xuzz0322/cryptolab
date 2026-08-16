.PHONY: run test

run:
	python3 -m quant_system

test:
	python3 -m unittest discover -v
