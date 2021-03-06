import os
from six import StringIO
import sys
import time
import threading

from dateutil.parser import parse
from dateutil.tz import tzlocal

from botocore.compat import quote
from awscli.customizations.s3.tasks import DownloadPartTask
from awscli.customizations.s3.utils import find_bucket_key, MultiCounter, \
    retrieve_http_etag, check_etag, check_error, operate, NoBlockQueue, \
    uni_print, guess_content_type


def make_last_mod_str(last_mod):
    """
    This function creates the last modified time string whenever objects
    or buckets are being listed
    """
    last_mod = parse(last_mod)
    last_mod = last_mod.astimezone(tzlocal())
    last_mod_tup = (str(last_mod.year), str(last_mod.month).zfill(2),
                    str(last_mod.day).zfill(2), str(last_mod.hour).zfill(2),
                    str(last_mod.minute).zfill(2),
                    str(last_mod.second).zfill(2))
    last_mod_str = "%s-%s-%s %s:%s:%s" % last_mod_tup
    return last_mod_str.ljust(19, ' ')


def make_size_str(size):
    """
    This function creates the size string when objects are being listed.
    """
    size_str = str(size)
    return size_str.rjust(10, ' ')


def read_file(filename):
    """
    This reads the file into a form that can be sent to S3
    """
    with open(filename, 'rb') as in_file:
        return in_file.read()


def save_file(filename, response_data, last_update):
    """
    This writes to the file upon downloading.  It reads the data in the
    response.  Makes a new directory if needed and then writes the
    data to the file.  It also modifies the last modified time to that
    of the S3 object.
    """
    data = response_data['Body'].read()
    etag = response_data['ETag'][1:-1]
    check_etag(etag, data)
    d = os.path.dirname(filename)
    try:
        if not os.path.exists(d):
            os.makedirs(d)
    except Exception:
        pass
    with open(filename, 'wb') as out_file:
        out_file.write(data)
    last_update_tuple = last_update.timetuple()
    mod_timestamp = time.mktime(last_update_tuple)
    os.utime(filename, (int(mod_timestamp), int(mod_timestamp)))


class TaskInfo(object):
    """
    This class contains important details related to performing a task.  This
    object is usually only used for creating buckets, removing buckets, and
    listing objects/buckets.  This object contains the attributes and
    functions needed to perform the task.  Note that just instantiating one
    of these objects will not be enough to run a listing or bucket command.
    unless ``session`` and ``region`` are specified upon instantiation.
    To make it fully operational, ``set_session`` needs to be used.
    This class is the parent class of the more extensive ``FileInfo`` object.

    :param src: the source path
    :type src: string
    :param src_type: if the source file is s3 or local.
    :type src_type: string
    :param operation: the operation being performed.
    :type operation: string
    :param session: ``botocore.session`` object
    :param region: The region for the endpoint

    Note that a local file will always have its absolute path, and a s3 file
    will have its path in the form of bucket/key
    """
    def __init__(self, src, src_type=None, operation=None, session=None,
                 region=None):
        self.src = src
        self.src_type = src_type
        self.operation = operation

        self.session = session
        self.region = region
        self.service = None
        self.endpoint = None
        if self.session and self.region:
            self.set_session(self.session, self.region)

    def set_session(self, session, region):
        """
        Given a session and region set the service and endpoint.  This enables
        operations to be performed as ``self.session`` is required to perform
        an operation.
        """
        self.session = session
        self.region = region
        self.service = self.session.get_service('s3')
        self.endpoint = self.service.get_endpoint(self.region)

    def list_objects(self):
        """
        List all of the buckets if no bucket is specified.  List the objects
        and common prefixes under a specified prefix.
        """
        bucket, key = find_bucket_key(self.src)
        if bucket == '':
            operation = self.service.get_operation('ListBuckets')
            html_response, response_data = operation.call(self.endpoint)
            header_str = "CreationTime".rjust(19, ' ')
            header_str = header_str + ' ' + "Bucket"
            underline_str = "------------".rjust(19, ' ')
            underline_str = underline_str + ' ' + "------"
            sys.stdout.write("\n%s\n" % header_str)
            sys.stdout.write("%s\n" % underline_str)
            buckets = response_data['Buckets']
            for bucket in buckets:
                last_mod_str = make_last_mod_str(bucket['CreationDate'])
                print_str = last_mod_str + ' ' + bucket['Name'] + '\n'
                uni_print(print_str)
                sys.stdout.flush()
        else:
            operation = self.service.get_operation('ListObjects')
            iterator = operation.paginate(self.endpoint, bucket=bucket,
                                          prefix=key, delimiter='/')
            sys.stdout.write("\nBucket: %s\n" % bucket)
            sys.stdout.write("Prefix: %s\n\n" % key)
            header_str = "LastWriteTime".rjust(19, ' ')
            header_str = header_str + ' ' + "Length".rjust(10, ' ')
            header_str = header_str + ' ' + "Name"
            underline_str = "-------------".rjust(19, ' ')
            underline_str = underline_str + ' ' + "------".rjust(10, ' ')
            underline_str = underline_str + ' ' + "----"
            sys.stdout.write("%s\n" % header_str)
            sys.stdout.write("%s\n" % underline_str)
            for html_response, response_data in iterator:
                check_error(response_data)
                common_prefixes = response_data['CommonPrefixes']
                contents = response_data['Contents']
                for common_prefix in common_prefixes:
                    prefix_components = common_prefix['Prefix'].split('/')
                    prefix = prefix_components[-2]
                    pre_string = "PRE".rjust(30, " ")
                    print_str = pre_string + ' ' + prefix + '/\n'
                    uni_print(print_str)
                    sys.stdout.flush()
                for content in contents:
                    last_mod_str = make_last_mod_str(content['LastModified'])
                    size_str = make_size_str(content['Size'])
                    filename_components = content['Key'].split('/')
                    filename = filename_components[-1]
                    print_str = last_mod_str + ' ' + size_str + ' ' + \
                        filename + '\n'
                    uni_print(print_str)
                    sys.stdout.flush()

    def make_bucket(self):
        """
        This opereation makes a bucket.
        """
        bucket, key = find_bucket_key(self.src)
        bucket_config = {'LocationConstraint': self.region}
        params = {'endpoint': self.endpoint, 'bucket': bucket}
        if self.region != 'us-east-1':
            params['create_bucket_configuration'] = bucket_config
        response_data, http = operate(self.service, 'CreateBucket', params)

    def remove_bucket(self):
        """
        This operation removes a bucket.
        """
        bucket, key = find_bucket_key(self.src)
        params = {'endpoint': self.endpoint, 'bucket': bucket}
        response_data, http = operate(self.service, 'DeleteBucket', params)


class FileInfo(TaskInfo):
    """
    This is a child object of the ``TaskInfo`` object.  It can perform more
    operations such as ``upload``, ``download``, ``copy``, ``delete``,
    ``move``.  Similiarly to
    ``TaskInfo`` objects attributes like ``session`` need to be set in order
    to perform operations.

    :param dest: the destination path
    :type dest: string
    :param compare_key: the name of the file relative to the specified
        directory/prefix.  This variable is used when performing synching
        or if the destination file is adopting the source file's name.
    :type compare_key: string
    :param size: The size of the file in bytes.
    :type size: integer
    :param last_update: the local time of last modification.
    :type last_update: datetime object
    :param dest_type: if the destination is s3 or local.
    :param dest_type: string
    :param parameters: a dictionary of important values this is assigned in
        the ``BasicTask`` object.
    """
    def __init__(self, src, dest=None, compare_key=None, size=None,
                 last_update=None, src_type=None, dest_type=None,
                 operation=None, session=None, region=None, parameters=None):
        super(FileInfo, self).__init__(src, src_type=src_type,
                                       operation=operation, session=session,
                                       region=region)
        self.dest = dest
        self.dest_type = dest_type
        self.compare_key = compare_key
        self.size = size
        self.last_update = last_update
        # Usually inject ``parameters`` from ``BasicTask`` class.
        if parameters:
            self.parameters = parameters
        else:
            self.parameters = {'acl': None,
                               'sse': None}

    def _permission_to_param(self, permission):
        if permission == 'read':
            return 'grant_read'
        if permission == 'full':
            return 'grant_full_control'
        if permission == 'readacl':
            return 'grant_read_acp'
        if permission == 'writeacl':
            return 'grant_write_acp'
        raise ValueError('permission must be one of: '
                         'read|readacl|writeacl|full')

    def _handle_object_params(self, params):
        if self.parameters['acl']:
            params['acl'] = self.parameters['acl'][0]
        if self.parameters['grants']:
            for grant in self.parameters['grants']:
                try:
                    permission, grantee = grant.split('=', 1)
                except ValueError:
                    raise ValueError('grants should be of the form '
                                     'permission=principal')
                params[self._permission_to_param(permission)] = grantee
        if self.parameters['sse']:
            params['server_side_encryption'] = 'AES256'
        if self.parameters['storage_class']:
            params['storage_class'] = self.parameters['storage_class'][0]
        if self.parameters['website_redirect']:
            params['website_redirect_location'] = self.parameters['website_redirect'][0]
        if self.parameters['guess_mime_type']:
            self._inject_content_type(params, self.src)
        if self.parameters['content_type']:
            params['content_type'] = self.parameters['content_type'][0]
        if self.parameters['cache_control']:
            params['cache_control'] = self.parameters['cache_control'][0]
        if self.parameters['content_disposition']:
            params['content_disposition'] = self.parameters['content_disposition'][0]
        if self.parameters['content_encoding']:
            params['content_encoding'] = self.parameters['content_encoding'][0]
        if self.parameters['content_language']:
            params['content_language'] = self.parameters['content_language'][0]
        if self.parameters['expires']:
            params['expires'] = self.parameters['expires'][0]

    def upload(self):
        """
        Redirects the file to the multipart upload function if the file is
        large.  If it is small enough, it puts the file as an object in s3.
        """
        body = read_file(self.src)
        bucket, key = find_bucket_key(self.dest)
        if sys.version_info[:2] == (2, 6):
            stream_body = StringIO(body)
        else:
            stream_body = bytearray(body)
        params = {'endpoint': self.endpoint, 'bucket': bucket, 'key': key}
        if body:
            params['body'] = stream_body
        self._handle_object_params(params)
        response_data, http = operate(self.service, 'PutObject', params)
        etag = retrieve_http_etag(http)
        check_etag(etag, body)


    def _inject_content_type(self, params, filename):
        # Add a content type param if we can guess the type.
        guessed_type = guess_content_type(filename)
        if guessed_type is not None:
            params['content_type'] = guessed_type

    def download(self):
        """
        Redirects the file to the multipart download function if the file is
        large.  If it is small enough, it gets the file as an object from s3.
        """
        bucket, key = find_bucket_key(self.src)
        params = {'endpoint': self.endpoint, 'bucket': bucket, 'key': key}
        response_data, http = operate(self.service, 'GetObject', params)
        save_file(self.dest, response_data, self.last_update)

    def copy(self):
        """
        Copies a object in s3 to another location in s3.
        """
        copy_source = quote(self.src.encode('utf-8'), safe='/~')
        bucket, key = find_bucket_key(self.dest)
        params = {'endpoint': self.endpoint, 'bucket': bucket,
                  'copy_source': copy_source, 'key': key}
        self._handle_object_params(params)
        response_data, http = operate(self.service, 'CopyObject', params)

    def delete(self):
        """
        Deletes the file from s3 or local.  The src file and type is used
        from the file info object.
        """
        if (self.src_type == 's3'):
            bucket, key = find_bucket_key(self.src)
            params = {'endpoint': self.endpoint, 'bucket': bucket, 'key': key}
            response_data, http = operate(self.service, 'DeleteObject',
                                          params)
        else:
            os.remove(self.src)

    def move(self):
        """
        Implements a move command for s3.
        """
        src = self.src_type
        dest = self.dest_type
        if src == 'local' and dest == 's3':
            self.upload()
        elif src == 's3' and dest == 's3':
            self.copy()
        elif src == 's3' and dest == 'local':
            self.download()
        else:
            raise Exception("Invalid path arguments for mv")
        self.delete()

    def create_multipart_upload(self):
        bucket, key = find_bucket_key(self.dest)
        params = {'endpoint': self.endpoint, 'bucket': bucket, 'key': key}
        self._handle_object_params(params)
        response_data, http = operate(self.service, 'CreateMultipartUpload',
                                      params)
        upload_id = response_data['UploadId']
        return upload_id
