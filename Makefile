all:
	${MAKE} -C lsoed
	${MAKE} -C kriek

demo: all topology.json
	sudo ./run-demo

ifneq (,$(wildcard topology.json))
FIND_NODES := 'import json; jj = json.load(open("topology.json")); print("\n".join([j[0] for j in jj] + [j[1] for j in jj]))'
CLEAN_NODES = $(foreach NODE,$(sort kriek $(shell python3 -c ${FIND_NODES})), docker stop ${NODE} &)
endif

clean:
	-${CLEAN_NODES} wait
	docker container prune -f
	rm -f topology.json topology.dot

topology.json:
	./generate-topology
