NODES := $(sort kriek $(shell awk '/^- / {gsub(/[,\[\]]/, ""); print $$2, $$3}' topology.yaml))

all:
ifeq (,$(wildcard lsoed/src/lsoed))
	git submodule update --init lsoed/src
endif
	${MAKE} -C lsoed
	${MAKE} -C kriek

demo: all
	sudo ./run-demo

clean:
	-for i in ${NODES}; do docker stop $$i & done; wait
	docker container prune -f
#	docker network prune -f
