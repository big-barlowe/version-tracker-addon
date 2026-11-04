ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip \
 && pip3 install --no-cache-dir --break-system-packages "paho-mqtt>=2.1,<3"

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh /app/vt-agent.sh

CMD [ "/run.sh" ]
