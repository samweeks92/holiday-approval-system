variable "github-host-connection-name" {
  type        = string
  description = "The name of the Github connection in Cloud Build"
}

variable "github-repo-name" {
  type        = string
  description = "The name of the Github repo"
}

variable "project" {
  type        = string
  description = "The GCP Project name to use for the deployments"
}

variable "region" {
  type        = string
  description = "The GCP Region to use for the deployments"
}

variable "demo-name" {
  description = "The chosen identifier for this demo/project. This is used for the subdomain name, the Cloud Run Frontend service name, the Cloud Run Frontend service external IP name and the Artifact Registry repo name."
  type        = string
}
