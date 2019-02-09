#FROM alpine:latest
#RUN apk add python3

FROM python:3.6-alpine

RUN mkdir /app

COPY requirements.txt /app
RUN  pip install -r /app/requirements.txt

COPY kriek templates /app/

#CMD ["/app/kriek", "--templates", "/app/templates"]
CMD /app/kriek --templates /app --host 0.0.0.0 1>/app/stdout 2>/app/stderr
