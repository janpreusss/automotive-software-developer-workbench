from constructs import Construct
from aws_cdk import (
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_codebuild as cb,
    aws_codepipeline as cp,
    aws_codepipeline_actions as cp_actions,
    aws_iam as iam,
    aws_s3 as s3,
    aws_ec2 as ec2,
    aws_cloudtrail as ct,
    aws_ecr as ecr
)
from pydantic import BaseModel
from typing import Optional, List
from src.workers import Workers, WorkersModel
from src.workbench import Workbench, WorkbenchModel

class VpcModel(BaseModel):
    ip_addresses: str = "10.1.0.0/16"
    
class ActionModel(BaseModel):
    name: str
    buildspec: str
    imageRepositoryArn: Optional[str] = None
    imageTag: Optional[str] = None
    
class StageModel(BaseModel):
    name: str
    actions: List[ActionModel]
    
class Artifacts(BaseModel):
    retain: bool = True

class Sourcecode(BaseModel):
    retain: bool = True

class Cloudtrail(BaseModel):
    retain: bool = True

class SoftwareFactoryModel(BaseModel):
    artifacts: Optional[Artifacts] = Artifacts()
    sourcecode: Optional[Sourcecode] = Sourcecode()
    cloudtrail: Optional[Cloudtrail] = Cloudtrail()
    vpc: Optional[VpcModel] = VpcModel()
    workers: Optional[WorkersModel] = None
    stages: Optional[List[StageModel]] = None
    workbench: Optional[WorkbenchModel] = None

class SoftwareFactoryStack(Stack):
  def __init__(self, scope: Construct, construct_id: str, 
        env_name: str, 
        project_name: str, 
        config: SoftwareFactoryModel, 
        **kwargs) -> None:
    super().__init__(scope, construct_id, **kwargs)
    
    account_id = Stack.of(self).account
    region = Stack.of(self).region
                
    CfnOutput(self, "Account ID", value=account_id, description='Account ID')

    kwargs = { 'bucket_name': f'{project_name}-{env_name}-{account_id}-{region}' }
    if not config.artifacts.retain:
        kwargs['removal_policy'] = RemovalPolicy.DESTROY
        kwargs['auto_delete_objects'] = True
        
    self.artifact = s3.Bucket(self, 'ArtifactBucket', **kwargs)

    kwargs = { 'bucket_name': f'{project_name}-{env_name}-sourcecode-{account_id}-{region}', 'versioned': True}
    if not config.sourcecode.retain:
        kwargs['removal_policy'] = RemovalPolicy.DESTROY
        kwargs['auto_delete_objects'] = True

    self.source_code = s3.Bucket(self, 'SourceCodeBucket', **kwargs)
    
    kwargs = { 'bucket_name': f'{project_name}-{env_name}-cloudtrail-{account_id}-{region}'}
    if not config.cloudtrail.retain:
        kwargs['removal_policy'] = RemovalPolicy.DESTROY
        kwargs['auto_delete_objects'] = True

    self.cloudtrail = s3.Bucket(self, 'LogBucket', **kwargs)
    
    self.vpc = ec2.Vpc(self, 'VPC',
        ip_addresses = ec2.IpAddresses.cidr(config.vpc.ip_addresses),
        enable_dns_hostnames = True,
        enable_dns_support = True,
        max_azs = 1,
        nat_gateways=0,
        subnet_configuration = [
                ec2.SubnetConfiguration(
                    cidr_mask = 24,
                    name = 'Public',
                    subnet_type = ec2.SubnetType.PUBLIC
                ),
                ec2.SubnetConfiguration(
                    cidr_mask=24,
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                )],
        gateway_endpoints={
            "S3": ec2.GatewayVpcEndpointOptions(
                service=ec2.GatewayVpcEndpointAwsService.S3)})
    
    self.vpc.add_interface_endpoint("SSM",
        service=ec2.InterfaceVpcEndpointAwsService.SSM,
        subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS))

    self.vpc.add_interface_endpoint("CW",
        service=ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS ,
        subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS))
        
    cb_role = iam.Role(self, 'CodeBuildRole', 
        assumed_by=iam.ServicePrincipal('codebuild.amazonaws.com'))
    cb_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name('AmazonEC2ContainerRegistryPullOnly'))
    self.artifact.grant_read_write(cb_role)
    self.source_code.grant_read_write(cb_role)

    if config.workers:
        workers = Workers(self, 'Workers', 
            env_name=env_name, 
            project_name=project_name,
            config=config.workers,
            vpc=self.vpc,
            artifact=self.artifact)
        if hasattr(workers, 'broker'):
            cb_role.add_to_policy(iam.PolicyStatement(
                    actions=['mq:ListBrokers'],
                    resources=['*']))
            workers.secret.grant_read(cb_role)
            self.source_code.grant_read_write(workers.role)

    pipeline = cp.Pipeline(self, 'Pipeline', 
        pipeline_name=f'{project_name}-{env_name}',
        cross_account_keys=False,
        artifact_bucket=self.artifact)
        
    source_stage = pipeline.add_stage(stage_name='Source')
    source_artifact = cp.Artifact()
    key = 'working-dir.zip'

    trail = ct.Trail(self, "CloudTrail", bucket=self.cloudtrail)
    trail.add_s3_event_selector([ct.S3EventSelector(
        bucket=self.source_code,
        object_prefix=key
    )],
        read_write_type=ct.ReadWriteType.WRITE_ONLY
    )

    source_action = cp_actions.S3SourceAction(
        action_name='S3Source',
        output=source_artifact,
        bucket=self.source_code,
        bucket_key=key,
        trigger=cp_actions.S3Trigger.EVENTS)

    source_stage.add_action(source_action)

    for stage in config.stages:
        actions = []
        for action in stage.actions:

            repository=None
            if action.imageRepositoryArn:
                repository=ecr.Repository.from_repository_arn(self, f'{action.name}Repo', action.imageRepositoryArn)

            kargs = {
                'role': cb_role,
                'environment': cb.BuildEnvironment(
                    compute_type=cb.ComputeType.SMALL,
                    build_image=(cb.LinuxBuildImage.from_ecr_repository(repository, action.imageTag) if repository else cb.LinuxBuildImage.AMAZON_LINUX_2_5)
                ),
                'build_spec': cb.BuildSpec.from_source_filename(f'.cb/{action.buildspec}'),
                'environment_variables': {
                    'SOURCE_CODE_BUCKET_NAME': cb.BuildEnvironmentVariable(
                        value=f'{self.source_code.bucket_name}'),
                    'ARTIFACT_BUCKET_NAME': cb.BuildEnvironmentVariable(
                        value=f'{self.artifact.bucket_name}'),
                    'WORKER_QUEUE_SECRET_REGION': cb.BuildEnvironmentVariable(
                        value=region),
                    'VERSION_ID': cb.BuildEnvironmentVariable(
                        value=source_action.variables.version_id),}}
            
            if config.workers and hasattr(workers, 'broker'):
                kargs.update({
                    'vpc': self.vpc,
                    'security_groups': [workers.sg],                    
                    'subnet_selection': ec2.SubnetSelection(
                        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)})
                kargs['environment_variables'].update({
                    'WORKER_QUEUE_BROKER_NAME': cb.BuildEnvironmentVariable(
                        value=workers.broker.broker_name),
                    'WORKER_QUEUE_SECRET_NAME': cb.BuildEnvironmentVariable(
                        value=workers.secret.secret_name)})
            
            actions.append(cp_actions.CodeBuildAction(
                action_name=action.name,
                input=source_artifact,
                project=cb.PipelineProject(self, action.name, **kargs)))
            
        pipeline.add_stage(stage_name=stage.name, actions=actions)
    
    if config.workbench:
        wb=Workbench(self, 'Workbench', 
            env_name=env_name, 
            project_name=project_name,
            config=config.workbench,
            vpc=self.vpc,
            artifact=self.artifact)
        wb.node.add_dependency(workers)
        self.source_code.grant_read_write(wb.role)