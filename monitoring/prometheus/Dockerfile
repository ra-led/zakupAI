FROM prom/prometheus:v2.53.0

COPY monitoring/prometheus/main.yml /etc/prometheus/main.yml

CMD ["--config.file=/etc/prometheus/main.yml", "--storage.tsdb.path=/prometheus", "--web.console.libraries=/usr/share/prometheus/console_libraries", "--web.console.templates=/usr/share/prometheus/consoles"]
