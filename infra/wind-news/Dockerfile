ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM node:22-bookworm-slim AS codex-cli
ARG CODEX_VERSION=0.159.2
RUN npm install --global @openai/codex@${CODEX_VERSION}
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 windnews && useradd --uid 10001 --gid windnews --create-home windnews
WORKDIR /app
COPY news/requirements.txt /app/news/requirements.txt
RUN pip install --no-cache-dir -r news/requirements.txt
COPY news/ /app/news/
COPY config/wind_news_collection.json config/wind_news_policy.json /app/config/
COPY schemas/ /app/schemas/
COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
USER 10001:10001
EXPOSE 8090
CMD ["python", "-m", "news"]
