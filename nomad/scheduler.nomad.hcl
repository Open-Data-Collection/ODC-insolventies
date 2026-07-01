job "insolventies-scheduler" {
  type = "batch"

  # Daily discovery sweep across all courts (matches the historical cadence).
  periodic {
    cron             = "0 6 * * *"
    prohibit_overlap = true
    time_zone        = "UTC"
  }

  # Runs next to Redis on odc-services.
  constraint {
    attribute = "${node.class}"
    value     = "services"
  }

  group "scheduler" {
    task "run" {
      driver = "docker"

      config {
        image      = "ghcr.io/open-data-collection/odc-insolventies:latest"
        args       = ["src.scheduler"]
        force_pull = true
      }

      template {
        destination = "secrets/secrets.env"
        env         = true
        change_mode = "restart"
        data        = <<EOH
CLICKHOUSE_PASSWORD={{with nomadVar "secrets/clickhouse"}}{{.odc_password}}{{end}}
REDIS_URL=redis://:{{with nomadVar "secrets/redis-services"}}{{.password}}{{end}}@services-redis:6379
EOH
      }

      env {
        PROJECT_NAME    = "insolventies"
        CLICKHOUSE_HOST = "clickhouse"
        CLICKHOUSE_USER = "odc"
        REQUEST_DELAY   = "1.0"
      }

      resources {
        cpu    = 200
        memory = 256
      }
    }
  }
}
