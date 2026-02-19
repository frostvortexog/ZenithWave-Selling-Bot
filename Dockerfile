FROM php:8.2-apache

# Enable Apache rewrite (optional but common)
RUN a2enmod rewrite

# Install Postgres PDO driver
RUN apt-get update && apt-get install -y \
    libpq-dev \
 && docker-php-ext-install pdo pdo_pgsql \
 && rm -rf /var/lib/apt/lists/*

# Copy your bot file
COPY index.php /var/www/html/index.php

# (Optional) Make Apache listen on Render's PORT
# Render sets $PORT, Apache default is 80. We'll switch to $PORT at runtime via entrypoint.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 80
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["apache2-foreground"]
