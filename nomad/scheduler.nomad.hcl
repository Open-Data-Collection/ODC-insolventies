job "insolventies-scheduler" {
  type = "batch"

  # Discovery sweeps on weekdays only — Dutch courts pronounce faillissementen
  # overwhelmingly on Tuesday (~54 vs 2-3 on other weekdays, 0 in the weekend),
  # and publish during business hours. Midday + end-of-day runs catch the
  # Tuesday wave same-day; the morning run mops up late/next-day publications.
  periodic {
    cron             = "0 7,12,17 * * 1-5"
    prohibit_overlap = true
    time_zone        = "Europe/Amsterdam"
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
CLICKHOUSE_PASSWORD={{with nomadVar "secrets/clickhouse-insolventies"}}{{.password}}{{end}}
REDIS_URL=redis://:{{with nomadVar "secrets/redis-services"}}{{.password}}{{end}}@services-redis:6379
EOH
      }

      env {
        PROJECT_NAME    = "insolventies"
        CLICKHOUSE_HOST = "clickhouse"
        CLICKHOUSE_USER = "insolventies"
        REQUEST_DELAY   = "1.0"
        # 3 runs/day now: the refresh anti-join is on last-scrape age, not queue
        # membership, so an undrained refresh batch would be re-queued by the
        # next run. 1000/run (3000/day potential) still far exceeds the ~700/day
        # steady-state need while keeping duplicate re-queues negligible.
        REFRESH_BATCH   = "1000"
      }

      resources {
        cpu    = 200
        memory = 256
      }
    }
  }
}
