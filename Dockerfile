FROM python:3.12-slim AS base
WORKDIR /app
COPY pyproject.toml constraints.txt /app/
RUN python -c "import pathlib,tomllib; p=tomllib.loads(pathlib.Path('pyproject.toml').read_text()); pathlib.Path('/tmp/requirements.txt').write_text('\n'.join(p['project']['dependencies'] + p['build-system']['requires']))" \
    && pip install --no-cache-dir -c constraints.txt -r /tmp/requirements.txt
COPY app /app/app
RUN pip install --no-cache-dir --no-build-isolation --no-deps .
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS geodata
RUN pip install --no-cache-dir -c /app/constraints.txt 'osmium==4.3.1'
CMD ["python", "-m", "app.geodata.build", "--source", "/data/moscow.osm.pbf", "--output", "/data/geodata.sqlite3"]

FROM base AS runtime
