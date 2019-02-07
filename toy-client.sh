#!/bin/sh -

# Proof of concept data uploader.  Real code would likely be Python,
# but this is a useful test.

: ${host=127.0.0.1} ${port=8080}

curl --header "Content-Type: application/json" \
     --request POST \
     --data '{"title" : "Testing", "body" : "Hello, CherryPy!"}' \
     http://${host}:${port}/mutate
