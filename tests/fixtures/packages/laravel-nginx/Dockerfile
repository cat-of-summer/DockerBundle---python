ARG PHP_VERSION
FROM php:${PHP_VERSION}
RUN apk add --no-cache supervisor dcron
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
