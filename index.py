import os
import re
import json
import traceback
from time import sleep
from urllib.parse import unquote

import boto3

quicksight = boto3.client('quicksight')
athena = boto3.client('athena')
s3 = boto3.client('s3')
dbName = os.getenv('AthenaDbName')
oputBucket = os.getenv('OutputBucket')
bucketFolders = [
    os.getenv('IamReportBucketFolder'),
    os.getenv('UsGrantsBucketFolder'),
    os.getenv('GamsBucketFolder'),
    os.getenv('AdUsersBucketFolder'),
    os.getenv('AdGroupsBucketFolder'),
]
qsDatasetNames = [
    os.getenv('IamQuicksightDatasetName'),
    os.getenv('UsGrantsQuicksightDatasetName'),
    os.getenv('GamsQuicksightDatasetName'),
    os.getenv('AdUsersQuicksightDatasetName'),
    os.getenv('AdGroupsQuicksightDatasetName'),
]
createTable = """
CREATE EXTERNAL TABLE IF NOT EXISTS
  {}.{} {}
  ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
  WITH SERDEPROPERTIES ({})
  LOCATION '{}'
  TBLPROPERTIES ('skip.header.line.count'='1');
"""


def queryStatus(qid):
    return athena.get_query_execution(QueryExecutionId=qid)['QueryExecution']['Status']['State']


def createColumns(columns):
    x = []
    for i in columns:
        i = i.strip().lower()
        i = re.sub('^"', '', i)
        i = re.sub('"$', '', i)
        x.append('`' + re.sub('[^a-z0-9-]+', '-', i) + '` string')
    return x


def wait4Query(qid):
    while queryStatus(qid) in ['QUEUED', 'RUNNING']:
        print("Waiting query id to finish {}".format(qid))
        sleep(5)
    print("query id finished {}".format(qid))


def run_query(query):
    print("Running query: {}".format(query))
    qid = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={'Database': dbName},
        ResultConfiguration={'OutputLocation': oputBucket}
    )['QueryExecutionId']
    print("query id: {}".format(qid))
    return qid


def updateIamQsAnalysis(newView, DataSetId):
    r = quicksight.describe_data_set(AwsAccountId=os.getenv('AwsAccountId'), DataSetId=DataSetId)
    Name = r['DataSet']['Name']
    PhysicalTableMap = r['DataSet']['PhysicalTableMap']
    LogicalTableMap = r['DataSet']['LogicalTableMap']
    ImportMode = r['DataSet']['ImportMode']
    LogicalTableMap[list(LogicalTableMap.keys())[0]]['Alias'] = newView
    PhysicalTableMap[list(PhysicalTableMap.keys())[0]]['RelationalTable']['Name'] = newView
    r = quicksight.update_data_set(
         AwsAccountId=os.getenv('AwsAccountId'),
         DataSetId=DataSetId,
         Name=Name,
         PhysicalTableMap=PhysicalTableMap,
         LogicalTableMap=LogicalTableMap,
         ImportMode=ImportMode
    )
    print(r)
    print("New Quicksight dataset created {}".format(newView))


def handler(event, context):
    print(json.dumps(event))

    try:
        query = "CREATE DATABASE IF NOT EXISTS {};".format(dbName)
        query_id = run_query(query)
        wait4Query(query_id)

        for r in event['Records']:
            bucket = r['s3']['bucket']['name']
            key = r['s3']['object']['key'].replace('+', ' ')
            if 'view' in key:  # Skip for files under view dir
                continue
            s3.put_object_tagging(
                Bucket=bucket,
                Key=key,
                Tagging={
                    'TagSet': [
                        {
                            'Key': 'Type',
                            'Value': 'AthenaDataSet'
                        }
                    ]
                }
            )
            csv_file = '/tmp/' + os.path.basename(key)
            csv_path = os.path.dirname(key)
            table_name = re.sub('[^a-z0-9_]+', '_', csv_path.split('/')[-1].lower())
            location = 's3://{}/{}/'.format(bucket, csv_path)
            s3.download_file(bucket, unquote(key), csv_file)
            columns = []
            serde_prop = ''
            with open(csv_file, 'r') as f:
                _ = f.readline()
                a = _.split('|')
                b = _.split(',')
                if len(a) > len(b):
                    serde_prop = "'separatorChar' = '|', 'serialization.format' = ',', 'field.delim' = '|'"
                    columns = createColumns(a)
                else:
                    serde_prop = "'serialization.format' = ',', 'field.delim' = ','"
                    columns = createColumns(b)
            columns = '(' + ', ' . join(columns) + ')'

            query = createTable.format(dbName, table_name, columns, serde_prop, location)
            query_id = run_query(query)
            wait4Query(query_id)

            x = csv_path.split('/')[:-1]
            x.append('views/')
            view_path = '/' . join(x)
            files = s3.list_objects(Bucket=bucket, Prefix=view_path)

            if 'Contents' in files.keys():
                for k, f in enumerate(files['Contents']):
                    if f['Key'].endswith('/'):
                        continue
                    view_file = '/tmp/' + os.path.basename(f['Key'])
                    s3.download_file(bucket, f['Key'], view_file)
                    with open(view_file, 'r') as v:
                        view_name = table_name + '_' + str(k) + '_view'
                        query = v.read().format(view_name, table_name)
                        query_id = run_query(query)
                        wait4Query(query_id)

            for k, v in enumerate(bucketFolders):
                if v in key:
                    isUpdated = False
                    args = {'AwsAccountId': os.getenv('AwsAccountId')}
                    while not isUpdated:
                        print('Finding dataset match for {}'.format(v))
                        r = quicksight.list_data_sets(**args)
                        for i in r['DataSetSummaries']:
                            if i['Name'] == qsDatasetNames[k]:
                                DataSetId = i['DataSetId']
                                updateIamQsAnalysis(view_name, DataSetId)
                                isUpdated = True
                                break
                        if 'NextToken' in r:
                            args['NextToken'] = r['NextToken']
                        else:
                            break
    except Exception as e:
        print(str(e))
        traceback.print_exc()
    return 0
