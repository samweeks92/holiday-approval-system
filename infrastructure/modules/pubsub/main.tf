data "google_project" "project" {
}

# 1. Pub/Sub Topics
resource "google_pubsub_topic" "dead_letter_topic" {
  name    = "${var.demo-name}-holiday-requests-dead-letter"
  project = var.project
}

resource "google_pubsub_topic" "main_topic" {
  name    = "${var.demo-name}-holiday-requests"
  project = var.project
}

# 2. Dedicated IAM Invoker Service Account for OIDC Push Authentication
resource "google_service_account" "pubsub_invoker_sa" {
  account_id   = "${substr(var.demo-name, 0, 9)}-pubsub-sa"
  display_name = "${var.demo-name} PubSub Invoker SA"
  description  = "Service account for Pub/Sub OIDC Push authentication to Agent Runtime"
  project      = var.project
}

# Grant Vertex AI User role to Invoker SA so it can query Reasoning Engine
resource "google_project_iam_member" "invoker_aiplatform_user" {
  project = var.project
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.pubsub_invoker_sa.email}"
}

# Grant Service Account Token Creator to Pub/Sub System Agent so it can mint OIDC tokens
resource "google_service_account_iam_member" "pubsub_sa_token_creator" {
  service_account_id = google_service_account.pubsub_invoker_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# Grant Publisher role on dead-letter topic to Pub/Sub System Agent
resource "google_pubsub_topic_iam_member" "pubsub_dead_letter_publisher" {
  project = var.project
  topic   = google_pubsub_topic.dead_letter_topic.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# Grant Subscriber role on main topic to Pub/Sub System Agent
resource "google_pubsub_topic_iam_member" "pubsub_main_subscriber" {
  project = var.project
  topic   = google_pubsub_topic.main_topic.name
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# 3. Authenticated OIDC Push Subscription delivering directly to Agent Runtime :query API
resource "google_pubsub_subscription" "push_subscription" {
  name                  = "${var.demo-name}-holiday-requests-push"
  topic                 = google_pubsub_topic.main_topic.name
  project               = var.project
  ack_deadline_seconds  = 600

  push_config {
    push_endpoint = "https://${var.region}-aiplatform.googleapis.com/v1/projects/${var.project}/locations/${var.region}/reasoningEngines/${var.agent-runtime-id}:streamQuery"

    no_wrapper {
      write_metadata = false
    }

    oidc_token {
      service_account_email = google_service_account.pubsub_invoker_sa.email
    }
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter_topic.id
    max_delivery_attempts = 5
  }

  depends_on = [
    google_service_account_iam_member.pubsub_sa_token_creator,
    google_pubsub_topic_iam_member.pubsub_dead_letter_publisher,
    google_pubsub_topic_iam_member.pubsub_main_subscriber
  ]
}
