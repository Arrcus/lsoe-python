NODES := $(sort kriek $(shell awk '/^- / {gsub(/[,\[\]]/, ""); print $$2, $$3}' topology.yaml))

all:
	${MAKE} -C lsoed
	${MAKE} -C kriek

test: all
	sudo ./run-test

clean:
	-for i in ${NODES}; do docker stop $$i; done
	docker container prune -f
	docker network prune -f
