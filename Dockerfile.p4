FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

ARG RELEASE_VERSION=1.2.0-p4-rc1
ENV P4_RELEASE_VERSION=$RELEASE_VERSION
WORKDIR /app
COPY requirements-p4-lock.txt /app/requirements-p4-lock.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r /app/requirements-p4-lock.txt
COPY card_studio /app/card_studio
COPY migrations /app/migrations
COPY staging /app/staging
RUN groupadd --system card && useradd --system --gid card card && mkdir -p /data && chown card:card /data
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER card
CMD ["python", "-m", "card_studio.p4.server"]
