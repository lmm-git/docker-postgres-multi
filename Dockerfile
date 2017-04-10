# vim:set ft=dockerfile:
FROM postgres:9.6

COPY docker-entrypoint.py /usr/local/bin/

ENTRYPOINT ["docker-entrypoint.py"]
CMD ["postgres"]
