#FROM alpine:latest
#RUN apk add python3

FROM python:3.6-alpine

RUN mkdir /app

COPY requirements.txt /app
RUN  pip install -r /app/requirements.txt

COPY configure waiter lsoed /app/

RUN  touch /app/blocked
#CMD ["/app/waiter"]
CMD /app/waiter 1>/app/stdout 2>/app/stderr
