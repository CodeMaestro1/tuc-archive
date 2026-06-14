# tuc-archive: TYPO3 tx_tucforum -> ZIM archiver
# Debian base so libzim + libmagic native deps install cleanly.
FROM python:3.12-slim

# Native deps:
#   libmagic1  -> python-magic (content-type sniffing)
#   libcairo2  -> cairosvg / cairocffi, imported by zimscraperlib 5.x image stack
#                 (SVG handling). Without it `import zimscraperlib.zim.creator`
#                 fails at load time with "no library called cairo-2".
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY tuc_archive ./tuc_archive

RUN pip install --no-cache-dir ".[distributed,dashboard]"

# default output volume
VOLUME ["/data"]
ENV TUC_OUTPUT=/data \
    TUC_STATE=/data/state.yml

ENTRYPOINT ["tuc-archive"]
CMD ["--help"]
