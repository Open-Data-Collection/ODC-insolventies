job "insolventies-worker" {
  type = "service"

  # Network-bound scraping runs on the processing fleet.
  constraint {
    attribute = "${node.class}"
    value     = "processing"
  }

  group "worker" {
    count = 3

    task "run" {
      driver = "docker"

      config {
        image      = "ghcr.io/open-data-collection/odc-insolventies:latest"
        args       = ["src.worker"]
        force_pull = true
      }

      template {
        destination = "secrets/secrets.env"
        env         = true
        change_mode = "restart"
        data        = <<EOH
CLICKHOUSE_PASSWORD={{with nomadVar "secrets/clickhouse-insolventies"}}{{.password}}{{end}}
REDIS_URL=redis://:{{with nomadVar "secrets/redis-services"}}{{.password}}{{end}}@services-redis:6379
STORAGE_MINIO_SECRET_KEY={{with nomadVar "secrets/minio-storage"}}{{.secret_key}}{{end}}
ANONYMIZATION_SALT={{with nomadVar "secrets/insolventies"}}{{.anonymization_salt}}{{end}}
EOH
      }

      env {
        PROJECT_NAME             = "insolventies"
        QUEUE_KEY                = "insolventies:tasks"
        CLICKHOUSE_HOST          = "clickhouse"
        CLICKHOUSE_USER          = "insolventies"
        STORAGE_MINIO_ENDPOINT   = "http://storage-minio:9002"
        STORAGE_MINIO_ACCESS_KEY = "minioadmin"
        REQUEST_DELAY            = "1.0"
        # Below redis-py 8's 5s default socket_timeout, or every idle BLPOP
        # raises TimeoutError instead of returning None (infra #40). Drop this
        # once the image rebuilds against odc-lib with the socket fix.
        POP_TIMEOUT_S            = "4"
      }

      resources {
        cpu    = 300
        memory = 384
      }
    }
  }
}
