#!/bin/sh
set -e
if [ ! -f "artisan" ]; then
  composer create-project laravel/laravel . --no-interaction
fi
php artisan migrate --force
exec "$@"
