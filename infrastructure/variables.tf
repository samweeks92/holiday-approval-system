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
  description = "The Google Cloud Project to deploy resources"
  default     = "ai-sandbox-sw"
}

variable "region" {
  type        = string
  description = "The Google Cloud Region to deploy resources"
  default     = "europe-west1"
}

variable "demo-name" {
  type        = string
  description = "The chosen identifier for this demo/project. This is used for the subdomain name, the Cloud Run Frontend service name, the Cloud Run Frontend service external IP name and the Artifact Registry repo name."
}

variable "tf-parallelism" {
  type        = string
  description = "The parallelism level for Terraform"
}

variable "agent-runtime-id" {
  type        = string
  description = "Deployed Agent Runtime Reasoning Engine ID"
  default     = "6128897715548979200"
}
