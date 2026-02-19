FROM php:8.2-cli

RUN apt-get update && apt-get install -y libpq-dev \
  && docker-php-ext-install pdo pdo_pgsql \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY index.php /app/index.php

# ✅ Render requires listening on $PORT
CMD ["sh", "-c", "php -S 0.0.0.0:${PORT} index.php"]
