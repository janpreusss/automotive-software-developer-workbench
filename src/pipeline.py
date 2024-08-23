import aws_cdk as cdk
from constructs import Construct
from aws_cdk.pipelines import CodePipeline, CodePipelineSource, ShellStep
from aws_cdk import (
    Stack,
    aws_iam as iam,
    aws_s3 as s3,
)
from src.stage import PipelineStage, PipelineStageModel


class PipelineStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str,
                 env_name: str,
                 config: PipelineStageModel,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account_id = Stack.of(self).account
        region = Stack.of(self).region

        key = 'working-dir.zip'
        source_code = s3.Bucket.from_bucket_name(self,
                                                 'SourceCodeBucket',
                                                 f'{config.project_name}-{env_name}-sourcecode-{account_id}-{region}')

        pipeline = CodePipeline(self, "Pipeline",
            pipeline_name="{}-infra".format(config.project_name),
            cross_account_keys=True,
            synth=ShellStep("Synth",
                input=CodePipelineSource.s3(source_code, object_key=key),
                commands=["npm install -g aws-cdk",
                          "python -m pip install -r requirements.txt",
                          "cdk synth"]))
        
        if config.test:
            env_test = cdk.Environment(
                account=config.test.account,
                region=config.test.region)
            pipeline.add_stage(
                PipelineStage(self, "{}-test".format(config.project_name), 
                        'test',
                        config.project_name,
                        config.test, 
                        env=env_test))
    
        if config.prod:
            env_prod = cdk.Environment(
                account=config.prod.account,
                region=config.prod.region)
            pipeline.add_stage(
                PipelineStage(self, "{}-test".format(config.project_name),
                        'prod',
                        config.project_name, 
                        config.prod, 
                        env=env_prod))
