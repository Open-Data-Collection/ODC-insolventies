job "insolventies-processor" {
  type = "batch"

  periodic {
    cron             = "*/30 * * * *"
    prohibit_overlap = true
    time_zone        = "UTC"
  }

  # Runs on odc-storage, next to ClickHouse — CH→CH shuffle stays local.
  # TEMP 2026-08-12: pinned to `services` instead — the storage node's Nomad
  # docker driver has a wedged image-pull coordinator for this image (allocs
  # hang in "Downloading image" forever, even with force_pull=false; manual
  # `docker pull` works fine). Revert to `storage` after
  # `systemctl restart nomad` on odc-storage clears the driver state.
  constraint {
    attribute = "${node.class}"
    value     = "services"
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
