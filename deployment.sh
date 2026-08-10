#!/bin/bash
set -e

# Load configuration
if [ -f "deployment_config.env" ]; then
    source deployment_config.env
else
    echo "Error: deployment_config.env not found. Please copy deployment_config.env.example to deployment_config.env and configure it."
    exit 1
fi

# 3. Set Org Policy (Allow all domains)
echo "🔒 Checking Domain Restricted Org Policy..."
CURRENT_POLICY=$(gcloud org-policies describe iam.allowedPolicyMemberDomains --project=$PROJECT_ID --format="value(spec.rules[0].allowAll)" 2>/dev/null || echo "")

if [ "$CURRENT_POLICY" = "true" ] || [ "$CURRENT_POLICY" = "True" ]; then
    echo "✅ Domain Restricted Org Policy is already set to allowAll."
else
    echo "🔓 Disabling Domain Restricted Org Policy..."
    cat <<EOF > policy.yaml
name: projects/$PROJECT_ID/policies/iam.allowedPolicyMemberDomains
spec:
  rules:
  - allowAll: true
EOF

    gcloud org-policies set-policy policy.yaml
    rm policy.yaml

    echo "Org policy set. Sleeping for 10 seconds to allow org policy to propagate..."
    sleep 10
fi

# 4. Create custom Cloud Build Service Account for CI/CD pipelines
echo "🤖 Checking custom Cloud Build Service Account for CI/CD pipelines..."
if ! gcloud iam service-accounts describe holiday-cloud-build-runner@$PROJECT_ID.iam.gserviceaccount.com --project=$PROJECT_ID > /dev/null 2>&1; then
    echo "   Creating custom Cloud Build Service Account..."
    gcloud iam service-accounts create holiday-cloud-build-runner --description="cloud-build-sa" --project=$PROJECT_ID --display-name="cloud-build-sa"

    # 6. Grant IAM permissions to the custom Cloud Build Service Account
    echo "🛡️ Granting IAM Permissions to the custom Cloud Build Service Account..."
    sleep 5
    ROLES=(
        "roles/aiplatform.user"
        "roles/artifactregistry.admin"
        "roles/ces.admin"
        "roles/cloudbuild.builds.editor"
        "roles/cloudbuild.connectionAdmin"
        "roles/cloudbuild.integrationsOwner"
        "roles/cloudbuild.tokenAccessor"
        "roles/compute.loadBalancerAdmin"
        "roles/compute.networkAdmin"
        "roles/datastore.owner"
        "roles/discoveryengine.admin"
        "roles/firebase.admin"
        "roles/iam.roleAdmin"
        "roles/iam.securityAdmin"
        "roles/iam.serviceAccountAdmin"
        "roles/iam.serviceAccountCreator"
        "roles/iam.serviceAccountUser"
        "roles/serviceusage.apiKeysAdmin"
        "roles/iap.admin"
        "roles/iap.settingsAdmin"
        "roles/logging.logWriter"
        "roles/oauthconfig.editor"
        "roles/resourcemanager.projectIamAdmin"
        "roles/run.admin"
        "roles/secretmanager.admin"
        "roles/secretmanager.secretAccessor"
        "roles/servicenetworking.serviceAgent"
        "roles/serviceusage.serviceUsageAdmin"
        "roles/storage.admin"
        "roles/storage.objectAdmin"
        "roles/vpcaccess.admin"
    )

    for role in "${ROLES[@]}"; do
        gcloud projects add-iam-policy-binding $PROJECT_ID \
            --member=serviceAccount:holiday-cloud-build-runner@$PROJECT_ID.iam.gserviceaccount.com \
            --condition=None \
            --role=$role > /dev/null
    done

    echo "IAM permissions granted to custom Cloud Build Service Account. Sleeping for 10 seconds to allow permissions to propagate..."
    sleep 5

else
    echo "✅ Custom Cloud Build Service Account already exists."
fi

# 5. Check GITHUB_PAT env var is set
echo "🔐 Checking GITHUB_PAT..."
if [ -z "$GITHUB_PAT" ]; then
    echo "Error: GITHUB_PAT is required. Please update deployment_config.env."
    exit 1
fi

# 6. Create the Secret Manager secrets for Github PAT Tokens and give service account permissions to access it
if ! gcloud secrets describe $GITHUB_PAT_SECRET_ID --project=$PROJECT_ID > /dev/null 2>&1; then
    echo "   Creating secret $GITHUB_PAT_SECRET_ID..."
    gcloud secrets create $GITHUB_PAT_SECRET_ID --project=$PROJECT_ID --replication-policy="automatic"
    echo "✅ Secret $GITHUB_PAT_SECRET_ID created."

    echo "Secret created. Sleeping for 10 seconds to allow secret to propagate..."
    sleep 10

    # Add new version of the secret
    echo "✅ creating secret version for $GITHUB_PAT_SECRET_ID..."
    echo -n "$GITHUB_PAT" | gcloud secrets versions add $GITHUB_PAT_SECRET_ID --project=$PROJECT_ID --data-file=-
    echo "✅ Secret $GITHUB_PAT_SECRET_ID version created"
    
    echo "Secret Version created. Sleeping for 10 seconds to allow secret version to propagate..."
    sleep 10

else
    echo "✅ Secret $GITHUB_PAT_SECRET_ID already exists."
fi

# 8. Grant default Cloud Build SA access to secrets (required for GitHub Host Connection creation)
echo "🔗 Granting default Cloud Build SA access to secrets..."
gcloud config set project $PROJECT_ID
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
SA_EMAIL="service-${PROJECT_NUMBER}@gcp-sa-cloudbuild.iam.gserviceaccount.com"

gcloud secrets add-iam-policy-binding $GITHUB_PAT_SECRET_ID \
    --project=$PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/secretmanager.secretAccessor"

CLOUD_BUILD_SERVICE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudbuild.iam.gserviceaccount.com"

gcloud secrets add-iam-policy-binding $GITHUB_PAT_SECRET_ID \
    --member="serviceAccount:${CLOUD_BUILD_SERVICE_AGENT}" \
    --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding $GITHUB_PAT_SECRET_ID \
    --member="serviceAccount:${CLOUD_BUILD_SERVICE_AGENT}" \
    --role="roles/secretmanager.admin"


# 7. Create the GitHub connection
echo "🔗 Checking GitHub Connection in region $REGION..."
if ! gcloud builds connections describe $GITHUB_HOST_CONNECTION_NAME --region=$REGION --project=$PROJECT_ID > /dev/null 2>&1; then
    echo "   Creating Github Connection..."
    gcloud builds connections create github $GITHUB_HOST_CONNECTION_NAME \
        --project=$PROJECT_ID \
        --region=$REGION \
        --authorizer-token-secret-version=projects/$PROJECT_ID/secrets/$GITHUB_PAT_SECRET_ID/versions/latest \
        --app-installation-id=$GITHUB_CLOUD_BUILD_APP_INSTALLATION_ID

    echo "GitHub Connection created. Sleeping for 10 seconds to allow GitHub connection to propagate..."
    sleep 10
else
    echo "✅ GitHub Connection already exists in $REGION."
fi

# 8. Link Github Repository
echo "🔗 Checking Repository Link in region $REGION..."
if ! gcloud builds repositories describe $GITHUB_REPO_NAME --connection=$GITHUB_HOST_CONNECTION_NAME --region=$REGION --project=$PROJECT_ID > /dev/null 2>&1; then
    echo "   Creating Repository Link for $GITHUB_REPO_NAME in $REGION..."
    gcloud builds repositories create $GITHUB_REPO_NAME \
    --remote-uri=$GITHUB_REPO_URI \
    --connection=$GITHUB_HOST_CONNECTION_NAME --region=$REGION

    echo "Repository Link created. Sleeping for 10 seconds to allow repository link to propagate..."
    sleep 10

else
    echo "✅ Repository $GITHUB_REPO_NAME already linked."
fi

# 8. Create GCS Bucket for Terraform State
echo "🪣 Checking Terraform State Bucket..."
if ! gsutil ls -b gs://$TF_STATE_BUCKET_NAME > /dev/null 2>&1; then
    echo "   Creating bucket gs://$TF_STATE_BUCKET_NAME..."
    gcloud storage buckets create gs://$TF_STATE_BUCKET_NAME --project=$PROJECT_ID
    gcloud storage buckets update gs://$TF_STATE_BUCKET_NAME --versioning
else
    echo "✅ Bucket gs://$TF_STATE_BUCKET_NAME already exists."
fi

# 9. Apply Build Trigger
echo "🔫 Checking Infrastructure Build Trigger..."
if ! gcloud builds triggers describe $DEMO_NAME-tf-apply --region=$REGION --project=$PROJECT_ID > /dev/null 2>&1; then
    echo "   Creating Build Trigger $DEMO_NAME-tf-apply..."
    gcloud builds triggers create github \
        --name=$DEMO_NAME-tf-apply \
        --region=$REGION \
        --service-account="projects/$PROJECT_ID/serviceAccounts/holiday-cloud-build-runner@$PROJECT_ID.iam.gserviceaccount.com" \
        --repository="projects/$PROJECT_ID/locations/$REGION/connections/$GITHUB_HOST_CONNECTION_NAME/repositories/$GITHUB_REPO_NAME" \
        --branch-pattern=main \
        --build-config=infrastructure/cloudbuild.yaml \
        --included-files="infrastructure/**" \
        --substitutions "_TF_STATE_BUCKET_NAME_=$TF_STATE_BUCKET_NAME,_GITHUB_HOST_CONNECTION_NAME_=$GITHUB_HOST_CONNECTION_NAME,_GITHUB_REPO_NAME_=$GITHUB_REPO_NAME,_PROJECT_ID_=$PROJECT_ID,_REGION_=$REGION,_DEMO_NAME_=$DEMO_NAME,_TF_PARALLELISM_=$TF_PARALLELISM"
else
    echo "✅ Build Trigger $DEMO_NAME-tf-apply already exists."
fi

# # 10. Run the Build Trigger
# echo "🏗️ Running Infrastructure Build Trigger..."
# # Capture the Build ID to wait for it (since triggers run doesn't support --wait directly)
# BUILD_ID=$(gcloud builds triggers run $DEMO_NAME-tf-apply --region=$REGION --project=$PROJECT_ID --branch=main --format="value(metadata.build.id)")

# echo "⏳ Waiting for Infrastructure Build $BUILD_ID to complete..."
# # Stream logs which effectively waits for completion
# gcloud builds log $BUILD_ID --project=$PROJECT_ID --region=$REGION --stream

echo "✅ Deployment Script Completed Successfully."