FROM alpine:3.18

ENV APPDIR=/srv/jobserv
ENV PYTHONPATH=$APPDIR
ENV FLASK_APP=jobserv.app:app

# Setup flask application
RUN mkdir -p $APPDIR

COPY ./requirements.txt /srv/jobserv/

RUN apk --no-cache add python3 py3-pip mysql-client python3-dev musl-dev g++ openssl libffi-dev openssl-dev linux-headers && \
	pip3 install -r $APPDIR/requirements.txt && \
	apk del python3-dev musl-dev g++ libffi-dev openssl-dev linux-headers

COPY ./ $APPDIR/
RUN cd $APPDIR/runner && python3 ./setup.py bdist_wheel

WORKDIR $APPDIR
EXPOSE 8000

ARG APP_VERSION=?
ENV APP_VERSION=$APP_VERSION

# Start gunicorn
CMD ["/srv/jobserv/docker_run.sh"]
