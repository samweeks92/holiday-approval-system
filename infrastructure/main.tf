module "frontend" {

  # Set Source
  source = "./modules/frontend"

  # Define Environment Variables
  github-host-connection-name = var.github-host-connection-name
  github-repo-name            = var.github-repo-name
  project                     = var.project
  region                      = var.region
  demo-name                   = var.demo-name

}
