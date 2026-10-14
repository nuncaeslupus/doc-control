# doc-control MCP server over stdio, file engine only (the live engine needs Word or
# LibreOffice on the host, so here it answers ENGINE_UNAVAILABLE).
#   docker build -t doc-control .
#   docker run -i --rm -v "$PWD:/work" doc-control
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY pyproject.toml README.md LICENSE* ./
COPY src ./src
RUN pip install --no-compile . && rm -rf /src

RUN useradd --create-home --uid 1000 app
USER app
WORKDIR /work

ENTRYPOINT ["doc-control"]
CMD ["mcp"]
