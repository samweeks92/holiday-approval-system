variable "project" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP Region"
  type        = string
}

variable "demo-name" {
  description = "Demo name prefix"
  type        = string
}

variable "agent-runtime-id" {
  description = "Deployed Agent Runtime Reasoning Engine ID"
  type        = string
  default     = "6128897715548979200"
}
