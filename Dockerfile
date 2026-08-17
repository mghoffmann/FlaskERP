# Dockerfile — app image for Shopfloor ERP.
#
# Built once, used both in dev (docker-compose.yml overrides `command:` to run
# `flask run --debug` with the source bind-mounted for live reload) and in prod
# (docker-compose.prod.yml runs this image as-is via entrypoint.sh -> gunicorn).

FROM python:3.12-slim

# Prevent Python from writing .pyc files and buffering stdout/stderr — both
# matter in containers: .pyc files are useless clutter in an ephemeral image,
# and unbuffered output means `docker logs` shows lines as they're printed
# instead of stuck in a buffer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# --- Dependencies layer -----------------------------------------------------
# Copy ONLY requirements.txt first and install before copying the rest of the
# app. Docker caches each instruction as a layer keyed on its inputs; as long
# as requirements.txt doesn't change, `pip install` is skipped on rebuilds
# even when application code changes constantly. Copying the whole app first
# would invalidate this layer on every code edit, forcing a full reinstall.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Non-root user -----------------------------------------------------------
# Running the container process as root is unnecessary risk: if the app is
# ever compromised (e.g. via a dependency vuln), root-in-container is one
# fewer barrier between the attacker and the host. Create a dedicated,
# unprivileged user and switch to it before running application code.
RUN useradd --create-home --uid 1000 appuser

# --- Application code --------------------------------------------------------
# Copied after the dependency layer so that editing app code doesn't bust the
# pip-install cache above.
COPY . .

# entrypoint.sh needs the execute bit; also hand ownership of /app to the
# non-root user now that all files are in place.
RUN chmod +x entrypoint.sh && chown -R appuser:appuser /app

USER appuser

# Documents the port gunicorn/flask listen on inside the container; actual
# host publishing happens in docker-compose.yml's `ports:` mapping.
EXPOSE 8000

# entrypoint.sh waits for the DB, runs migrations, then execs whatever CMD
# (below) or docker-compose `command:` override it's given. See entrypoint.sh
# for why it's `exec`'d rather than a spawned subprocess.
ENTRYPOINT ["./entrypoint.sh"]

# Default command run by entrypoint.sh's final `exec "$@"` — production's
# gunicorn server. docker-compose.yml's dev `app` service overrides this via
# `command:` to run `flask run --debug` instead, for live reload; prod
# (docker-compose.prod.yml) leaves this default untouched.
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "app:create_app()"]
