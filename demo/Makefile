all: kriek.tar lsoed.tar
	docker build -t kriek - < kriek.tar
	docker build -t lsoed - < lsoed.tar

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
	tar -C kriek -cvvf $@ .

lsoed.tar: $(shell find lsoed -type f -print) ../lsoed
	tar -C lsoed -cvvf $@ .
	tar -C ..    -rvvf $@ lsoed
