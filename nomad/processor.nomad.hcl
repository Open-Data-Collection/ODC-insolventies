job "insolventies-processor" {
  type = "batch"

  periodic {
    cron             = "*/30 * * * *"
    prohibit_overlap = true
    time_zone        = "UTC"
  }

  # Runs on odc-storage, next to ClickHouse — CH→CH shuffle stays local.
  constraint {
    attribute = "${node.class}"
    value     = "storage"
  }

  group "processor" {
    task "run" {
      driver = "docker"

      config {
        image      = "ghcr.io/open-data-collection/odc-insolventies:latest"
        args       = ["src.processor"]
        force_pull = true
      }

      template {
        destination = "secrets/secrets.env"
        env         = true
        change_mode = "restart"
        data        = <<EOH
CLICKHOUSE_PASSWORD={{with nomadVar "secrets/clickhouse-insolventies"}}{{.password}}{{end}}
EOH
      }

      env {
        PROJECT_NAME    = "insolventies"
        CLICKHOUSE_HOST = "clickhouse"
        CLICKHOUSE_USER = "insolventies"
      }

      resources {
        cpu    = 400
        memory = 512
      }
    }
  }
}
