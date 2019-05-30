all: kriek.tar lsoed.tar

test: start-demo

start-demo: all topology.json
	sudo ./run-demo

stop-demo:
	-$(foreach NODE,$(shell ./demo-nodes), docker stop ${NODE} &) wait
	docker container prune -f

clean: stop-demo
	rm -f topology.json topology.dot kriek.tar lsoed.tar

topology.json:
	./generate-topology

kriek.tar: $(shell find kriek -type f -print)
	tar -C $(basename $@) -cvvf $@ .
	docker build -t $(basename $@) - < $@

lsoed.tar: $(shell find lsoed -type f -print) ../lsoed
	tar -C $(basename $@) -cvvf $@ .
	tar -C ..             -rvvf $@ lsoed
	docker build -t $(basename $@) - < $@

.DELETE_ON_ERROR: kriek.tar lsoed.tar

.PHONY: all test start-demo stop-demo clean
