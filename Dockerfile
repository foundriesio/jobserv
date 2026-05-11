FROM python:3.14.3-alpine3.23

ENV APPDIR=/srv/jobserv
ENV PYTHONPATH=$APPDIR
ENV FLASK_APP=jobserv.app:app

# Setup flask application
RUN mkdir -p $APPDIR

COPY ./requirements.txt /srv/jobserv/

RUN apk --no-cache add mysql-client musl-dev g++ openssl libffi-dev openssl-dev linux-headers mariadb-connector-c && \
	pip3 install --break-system-packages -r $APPDIR/requirements.txt && \
	apk del musl-dev g++ libffi-dev openssl-dev linux-headers

COPY ./ $APPDIR/
RUN cd $APPDIR/runner && python3 ./setup.py bdist_wheel

WORKDIR $APPDIR
EXPOSE 8000

ARG APP_VERSION=?
ENV APP_VERSION=$APP_VERSION

# Start gunicorn
CMD ["/srv/jobserv/docker_run.sh"]
