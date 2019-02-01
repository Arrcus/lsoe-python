#!/bin/sh -

# Proof of concept data uploader.  Real code would likely be Python,
# but this is a useful test.

curl --header "Content-Type: application/json" \
     --request POST \
     --data '{"title" : "Testing", "body" : "Hello, CherryPy!"}' \
     http://127.0.0.1:8080/mutate
