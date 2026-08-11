data "google_project" "project" {
}

##########################
##########################
# Part 1: Frontend Image #
##########################
##########################

resource "google_artifact_registry_repository" "frontend" {
  location      = var.region
  repository_id = "${var.demo-name}-holiday-frontend"
  description   = "Managed by Terraform - Do not manually edit - ${var.demo-name} holiday frontend repository"
  format        = "DOCKER"

  docker_config {
    immutable_tags = false
  }

  lifecycle {
    ignore_changes = [docker_config]
  }
}

resource "google_cloudbuild_trigger" "frontend" {
  project         = var.project
  location        = var.region
  name            = "${var.demo-name}-holiday-frontend-build"
  description     = "Managed by Terraform - Do not manually edit - ${var.demo-name} holiday frontend image build"
  service_account = "projects/${var.project}/serviceAccounts/holiday-cloud-build-runner@${var.project}.iam.gserviceaccount.com"

  repository_event_config {
    repository = "projects/${var.project}/locations/${var.region}/connections/${var.github-host-connection-name}/repositories/${var.github-repo-name}"
    push {
      branch = "main"
    }
  }

  included_files = ["frontend/**"]

  substitutions = {
    _PROJECT_ID_     = var.project
    _REGION_         = var.region
    _DEMO_NAME_      = var.demo-name
    _GAR_REPOSITORY_ = google_artifact_registry_repository.frontend.repository_id
  }

  filename = "frontend/cloudbuild.yaml"
}

# Artifact Registry for User Portal App
resource "google_artifact_registry_repository" "user_app" {
  location      = var.region
  repository_id = "${var.demo-name}-holiday-user-app"
  description   = "Managed by Terraform - Do not manually edit - ${var.demo-name} holiday user portal repository"
  format        = "DOCKER"

  docker_config {
    immutable_tags = false
  }

  lifecycle {
    ignore_changes = [docker_config]
  }
}

# Cloud Build Trigger for User Portal App
resource "google_cloudbuild_trigger" "user_app" {
  project         = var.project
  location        = var.region
  name            = "${var.demo-name}-holiday-user-app-build"
  description     = "Managed by Terraform - Do not manually edit - ${var.demo-name} holiday user portal image build"
  service_account = "projects/${var.project}/serviceAccounts/holiday-cloud-build-runner@${var.project}.iam.gserviceaccount.com"

  repository_event_config {
    repository = "projects/${var.project}/locations/${var.region}/connections/${var.github-host-connection-name}/repositories/${var.github-repo-name}"
    push {
      branch = "main"
    }
  }

  included_files = ["frontend-user-app/**"]

  substitutions = {
    _PROJECT_ID_     = var.project
    _REGION_         = var.region
    _DEMO_NAME_      = var.demo-name
    _GAR_REPOSITORY_ = google_artifact_registry_repository.user_app.repository_id
  }

  filename = "frontend-user-app/cloudbuild.yaml"
}

#############################
#############################
# Part 2: Cloud Run service #
#############################
#############################

# Manager Portal Service Account
resource "google_service_account" "frontend-service-account" {
  account_id   = "${substr(var.demo-name, 0, 9)}-holiday-frontend-sa"
  display_name = "${substr(var.demo-name, 0, 9)} holiday Frontend SA"
  description  = "Service Account for the ${var.demo-name} holiday Frontend Service"
}

resource "google_project_iam_member" "frontend-sa-cloud-run-invoker-role" {
  project = var.project
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.frontend-service-account.email}"
}

resource "google_project_iam_member" "frontend-sa-aiplatform-admin-role" {
  project = var.project
  role    = "roles/aiplatform.admin"
  member  = "serviceAccount:${google_service_account.frontend-service-account.email}"
}

resource "google_project_iam_member" "frontend-sa-datastore-user" {
  project = var.project
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.frontend-service-account.email}"
}

resource "google_service_account_iam_member" "custom-cloud-run-sa-act-as-cloud-run-sa-frontend" {
  service_account_id = google_service_account.frontend-service-account.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:holiday-cloud-build-runner@${var.project}.iam.gserviceaccount.com"
}

# Manager Portal Cloud Run Service
resource "google_cloud_run_v2_service" "frontend-service" {
  provider     = google-beta
  launch_stage = "BETA"
  name         = "${var.demo-name}-holiday-frontend"
  location     = var.region
  ingress      = "INGRESS_TRAFFIC_ALL"
  template {
    labels = {
      managed-by = "terraform"
    }
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    timeout         = "300s"
    service_account = google_service_account.frontend-service-account.email
    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"
      env {
        name  = "PROJECT_ID"
        value = var.project
      }
      env {
        name  = "LOCATION"
        value = var.region
      }
      env {
        name  = "DEMO_NAME"
        value = var.demo-name
      }
      ports {
        container_port = 8080
      }
      resources {
        limits = {
          cpu    = "4",
          memory = "4Gi"
        }
      }
    }
    max_instance_request_concurrency = 80
  }

  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].containers[0].image
    ]
  }

  deletion_protection = false
  depends_on          = [google_service_account_iam_member.custom-cloud-run-sa-act-as-cloud-run-sa-frontend]
}

# User Portal App Service Account
resource "google_service_account" "user_app_sa" {
  account_id   = "${substr(var.demo-name, 0, 9)}-holiday-userapp-sa"
  display_name = "${substr(var.demo-name, 0, 9)} holiday User Portal SA"
  description  = "Service Account for the ${var.demo-name} holiday User Portal Service"
}

resource "google_project_iam_member" "user_app_sa_pubsub_publisher" {
  project = var.project
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.user_app_sa.email}"
}

resource "google_project_iam_member" "user_app_sa_datastore_user" {
  project = var.project
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.user_app_sa.email}"
}

resource "google_service_account_iam_member" "custom-cloud-run-sa-act-as-userapp" {
  service_account_id = google_service_account.user_app_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:holiday-cloud-build-runner@${var.project}.iam.gserviceaccount.com"
}

resource "google_project_iam_member" "user_app_sa_vertex_ai_user" {
  project = var.project
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.user_app_sa.email}"
}


# User Portal App Cloud Run Service
resource "google_cloud_run_v2_service" "user_app_service" {
  provider     = google-beta
  launch_stage = "BETA"
  name         = "${var.demo-name}-holiday-user-app"
  location     = var.region
  ingress      = "INGRESS_TRAFFIC_ALL"
  template {
    labels = {
      managed-by = "terraform"
    }
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    timeout         = "300s"
    service_account = google_service_account.user_app_sa.email
    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"
      env {
        name  = "PROJECT_ID"
        value = var.project
      }
      env {
        name  = "LOCATION"
        value = var.region
      }
      env {
        name  = "DEMO_NAME"
        value = var.demo-name
      }
      ports {
        container_port = 8080
      }
      resources {
        limits = {
          cpu    = "1",
          memory = "1Gi"
        }
      }
    }
    max_instance_request_concurrency = 80
  }

  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].containers[0].image
    ]
  }

  deletion_protection = false
  depends_on          = [google_service_account_iam_member.custom-cloud-run-sa-act-as-userapp]
}
