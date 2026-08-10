/**
 * Copyright 2023 Google LLC
 */

resource "google_project_service" "enable-cloud-resource-manager-api" {

  project = var.project
  service = "cloudresourcemanager.googleapis.com"

  timeouts {
    create = "15m"
    update = "15m"
  }

  disable_dependent_services = false
  disable_on_destroy         = false

}

resource "google_project_service" "enable-required-apis" {

  for_each = toset([
    "agentregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "apikeys.googleapis.com",
    "apphub.googleapis.com",
    "apptopology.googleapis.com",
    "artifactregistry.googleapis.com",
    "bigquery.googleapis.com",
    # "ces.googleapis.com",
    "cloudapiregistry.googleapis.com",
    # "dialogflow.googleapis.com",
    "discoveryengine.googleapis.com",
    "dns.googleapis.com",
    "firebaserules.googleapis.com",
    "firestore.googleapis.com",
    "iamconnectors.googleapis.com",
    "iap.googleapis.com",
    "identitytoolkit.googleapis.com",
    "modelarmor.googleapis.com",
    "monitoring.googleapis.com",
    "networkconnectivity.googleapis.com",
    "networksecurity.googleapis.com",
    "networkservices.googleapis.com",
    "notebooks.googleapis.com",
    "observability.googleapis.com",
    "run.googleapis.com",
    "saasservicemgmt.googleapis.com",
    "securitycenter.googleapis.com",
    "servicenetworking.googleapis.com",
    "texttospeech.googleapis.com",
    "vpcaccess.googleapis.com",
  ])
  project = var.project
  service = each.value

  timeouts {
    create = "15m"
    update = "15m"
  }

  disable_dependent_services = false
  disable_on_destroy         = false

  depends_on = [
    google_project_service.enable-cloud-resource-manager-api
  ]

}