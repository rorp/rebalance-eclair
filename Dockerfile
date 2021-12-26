FROM python:3.10-alpine3.14 

RUN apk add --update --no-cache linux-headers gcc g++ git openssh-client \
    && apk add libstdc++ --no-cache --repository http://dl-3.alpinelinux.org/alpine/edge/testing/ --allow-untrusted \
    && git clone --depth=1 https://github.com/rorp/rebalance-eclair.git \
    && rm -rf rebalance-eclair/.github \
    && cd rebalance-eclair \
    && /usr/local/bin/python -m pip install --upgrade pip \
    && pip install -r requirements.txt \
    && cd / \
    && apk del linux-headers gcc g++ git openssh-client \
    && mkdir /root/.eclair

VOLUME [ "/root/.eclair" ]

WORKDIR /

ENTRYPOINT [ "/rebalance-eclair/rebalance.py" ]
