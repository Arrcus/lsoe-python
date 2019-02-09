all:
	${MAKE} -C lsoed
	${MAKE} -C kriek

test: all
	./run-test
