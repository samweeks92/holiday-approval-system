# Firestore Database for Holiday Approvals System
resource "google_firestore_database" "holiday_data" {
  project     = var.project
  name        = "holiday-data"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  deletion_policy = "DELETE"

  depends_on = [
    google_project_service.enable-required-apis
  ]
}

# Grant Datastore/Firestore User role to project service accounts for database access
resource "google_project_iam_member" "firestore_user_frontend" {
  project = var.project
  role    = "roles/datastore.user"
  member  = "serviceAccount:holiday-holiday-frontend-sa@${var.project}.iam.gserviceaccount.com"

  depends_on = [
    module.frontend
  ]
}

resource "google_project_iam_member" "firestore_user_agent" {
  project = var.project
  role    = "roles/datastore.user"
  member  = "serviceAccount:service-110071138645@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
}
