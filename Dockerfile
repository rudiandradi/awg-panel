FROM python:3.12-alpine

WORKDIR /app
RUN apk add --no-cache libqrencode-tools
COPY server.py /app/server.py
COPY static /app/static

EXPOSE 8080
CMD ["python", "/app/server.py"]
