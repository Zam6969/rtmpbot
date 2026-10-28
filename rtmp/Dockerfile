FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends nginx libnginx-mod-rtmp \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /recordings && chown www-data:www-data /recordings
COPY nginx.conf /etc/nginx/nginx.conf
EXPOSE 1935
CMD ["nginx", "-g", "daemon off;"]
