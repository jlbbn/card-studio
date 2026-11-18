#!/usr/bin/env bash
set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-east-2}"
export AWS_PAGER=""

command -v aws >/dev/null

printf '\n[A] IAM / ROOT — metadata only\n'
aws iam get-account-summary \
  --query 'SummaryMap.{RootMFAEnabled:AccountMFAEnabled,RootAccessKeysPresent:AccountAccessKeysPresent,Users:Users,Roles:Roles}'
aws iam list-users \
  --query 'Users[].{UserName:UserName,CreateDate:CreateDate}'
aws iam list-roles \
  --query 'Roles[].{RoleName:RoleName,CreateDate:CreateDate}'
aws iam list-policies --scope Local \
  --query 'Policies[].{PolicyName:PolicyName,PolicyId:PolicyId,DefaultVersionId:DefaultVersionId}'
aws sso-admin list-instances \
  --query 'Instances[].{IdentityStoreId:IdentityStoreId,InstanceArn:InstanceArn}'

# Metadata de access keys por usuario, sin imprimir el AccessKeyId:
# aws iam list-access-keys --user-name "$IAM_USER" \
#   --query 'AccessKeyMetadata[].{Status:Status,CreateDate:CreateDate}'
# Política concreta para least privilege; no contiene secretos por diseño, revisar antes de publicar:
# aws iam get-policy-version --policy-arn "$POLICY_ARN" --version-id "$POLICY_VERSION"
# Identidades operativas, después de list-instances:
# aws identitystore list-users --identity-store-id "$IDENTITY_STORE_ID" \
#   --query 'Users[].{UserName:UserName,UserId:UserId}'
# aws identitystore list-groups --identity-store-id "$IDENTITY_STORE_ID" \
#   --query 'Groups[].{DisplayName:DisplayName,GroupId:GroupId}'

printf '\n[A/H] Task and execution role identifiers — metadata only\n'
aws ecs describe-task-definition --task-definition sunmi-v3-cobros:7 \
  --query 'taskDefinition.{Family:family,Revision:revision,TaskRole:taskRoleArn,ExecutionRole:executionRoleArn}'

printf '\n[B/G] RDS backups and restore readiness — configuration only\n'
aws rds describe-db-instances --db-instance-identifier swiftledger-db \
  --query 'DBInstances[0].{ResourceId:DbiResourceId,Retention:BackupRetentionPeriod,Window:PreferredBackupWindow,DeletionProtection:DeletionProtection,CopyTagsToSnapshot:CopyTagsToSnapshot,MultiAZ:MultiAZ,Encrypted:StorageEncrypted,PubliclyAccessible:PubliclyAccessible}'
aws rds describe-db-snapshots --db-instance-identifier swiftledger-db --snapshot-type automated \
  --max-records 20 \
  --query 'reverse(sort_by(DBSnapshots,&SnapshotCreateTime))[:5].{Snapshot:DBSnapshotIdentifier,Created:SnapshotCreateTime,Status:Status,Encrypted:Encrypted}'
aws rds describe-db-instance-automated-backups \
  --query 'reverse(sort_by(DBInstanceAutomatedBackups,&RetainWindow))[:5].{ResourceId:DbiResourceId,Arn:DBInstanceAutomatedBackupsArn,Status:Status,RetainWindow:RetainWindow}'

printf '\n[C] CloudWatch alarms and metric inventory\n'
aws cloudwatch describe-alarms --alarm-types MetricAlarm CompositeAlarm \
  --query '{Metric:MetricAlarms[].{Name:AlarmName,State:StateValue,Namespace:Namespace,Metric:MetricName,Dimensions:Dimensions},Composite:CompositeAlarms[].{Name:AlarmName,State:StateValue}}'
aws cloudwatch list-metrics --namespace AWS/ECS \
  --dimensions Name=ClusterName,Value=sunmi-v3 Name=ServiceName,Value=sunmi-pos
aws cloudwatch list-metrics --namespace AWS/ECS \
  --dimensions Name=ClusterName,Value=swiftledger Name=ServiceName,Value=swiftledger-app
aws cloudwatch list-metrics --namespace AWS/RDS \
  --dimensions Name=DBInstanceIdentifier,Value=swiftledger-db
aws elbv2 describe-load-balancers \
  --query 'LoadBalancers[].{Name:LoadBalancerName,Scheme:Scheme,DNSName:DNSName}'
aws elbv2 describe-target-groups \
  --query 'TargetGroups[].{Name:TargetGroupName,Port:Port,Protocol:Protocol}'
# Inventario ALB/5xx una vez identificado el LoadBalancerArn:
# aws cloudwatch list-metrics --namespace AWS/ApplicationELB \
#   --dimensions Name=LoadBalancer,Value="$ALB_DIMENSION"

printf '\n[D] Secrets Manager / KMS — metadata only, never secret content\n'
aws secretsmanager list-secrets \
  --query 'SecretList[].{Name:Name,RotationEnabled:RotationEnabled,LastChangedDate:LastChangedDate,KmsKeyId:KmsKeyId}'
# Metadata de un secreto concreto:
# aws secretsmanager describe-secret --secret-id "$SECRET_ID" \
#   --query '{Name:Name,RotationEnabled:RotationEnabled,LastChangedDate:LastChangedDate,KmsKeyId:KmsKeyId}'
# Metadata KMS solamente si KMS_KEY_ID está autorizado:
# aws kms describe-key --key-id "$KMS_KEY_ID" \
#   --query 'KeyMetadata.{KeyId:KeyId,State:KeyState,Origin:Origin,KeyManager:KeyManager}'

printf '\n[E] CloudTrail configuration and delivery status\n'
aws cloudtrail describe-trails --include-shadow-trails false \
  --query 'trailList[].{Name:Name,MultiRegion:IsMultiRegionTrail,Bucket:S3BucketName,LogValidation:LogFileValidationEnabled}'
if [[ -n "${CLOUDTRAIL_NAME:-}" ]]; then
  aws cloudtrail get-trail-status --name "$CLOUDTRAIL_NAME" \
    --query '{Logging:IsLogging,LastDelivery:LatestDeliveryTime,LastError:LatestDeliveryError,LastNotification:LatestNotificationTime,LastNotificationError:LatestNotificationError}'
else
  printf 'Set CLOUDTRAIL_NAME after describe-trails to query delivery status.\n'
fi
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=ConsoleLogin \
  --max-results 50 \
  --query 'Events[].{EventTime:EventTime,EventName:EventName,Username:Username}'

printf '\n[F] AWS Config recorder, delivery, rules and compliance\n'
aws configservice describe-configuration-recorders \
  --query 'ConfigurationRecorders[].{Name:name,Role:roleARN,RecordingGroup:recordingGroup}'
aws configservice describe-configuration-recorder-status \
  --query 'ConfigurationRecordersStatus[].{Name:name,Recording:recording,LastStatus:lastStatus,LastStartTime:lastStartTime,LastStopTime:lastStopTime}'
aws configservice describe-delivery-channels \
  --query 'DeliveryChannels[].{Name:name,Bucket:s3BucketName,SnsTopic:snsTopicARN,Frequency:configSnapshotDeliveryProperties.deliveryFrequency}'
aws configservice describe-config-rules \
  --query 'ConfigRules[].{Name:ConfigRuleName,State:ConfigRuleState,Source:Source.Owner}'
aws configservice get-compliance-summary-by-config-rule \
  --query 'ComplianceSummary.{Compliant:ComplianceByConfigRule.CompliantResourceCount,NonCompliant:ComplianceByConfigRule.NonCompliantResourceCount}'

printf '\n[G] Restore readiness remains documentary; no restore command is included.\n'
printf 'Review docs/BACKUP_RECOVERY_P3.md and the RDS configuration above.\n'
