import os
import datetime
import subprocess
import time
import shutil
import json
import tempfile

from odoo import models, fields, api, tools, _
from odoo.exceptions import UserError
from odoo.tools import osutil
from odoo.tools.misc import exec_pg_environ, find_pg_tool
import odoo

import logging

_logger = logging.getLogger(__name__)

try:
    import paramiko
except ImportError:
    raise ImportError(
        'This module needs paramiko to automatically write backups to the FTP through SFTP. '
        'Please install paramiko on your system. (sudo pip3 install paramiko)')


class DbBackup(models.Model):
    _name = 'db.backup'
    _description = 'Backup configuration record'

    @api.model
    def _get_db_name(self):
        return self.env.cr.dbname

    # Columns for local server configuration
    host = fields.Char('Host', required=True, default='localhost')
    port = fields.Char('Port', required=True, default=lambda self: tools.config.get('http_port', 8069))
    name = fields.Char('Database', required=True, help='Database you want to schedule backups for',
                       default=_get_db_name)
    folder = fields.Char('Backup Directory', help='Absolute path for storing the backups', required=True,
                       default='/odoo/backups')
    backup_type = fields.Selection([('zip', 'Zip'), ('dump', 'Dump')], 'Backup Type', required=True, default='zip')
    autoremove = fields.Boolean('Auto. Remove Backups',
                                help='If you check this option you can choose to automaticly remove the backup '
                                     'after xx days')
    days_to_keep = fields.Integer('Remove after x days',
                                  help="Choose after how many days the backup should be deleted. For example:\n"
                                       "If you fill in 5 the backups will be removed after 5 days.",
                                  required=True)

    # Columns for external server (SFTP)
    sftp_write = fields.Boolean('Write to external server with sftp',
                                help="If you check this option you can specify the details needed to write to a remote "
                                     "server with SFTP.")
    sftp_path = fields.Char('Path external server',
                            help='The location to the folder where the dumps should be written to. For example '
                                 '/odoo/backups/.\nFiles will then be written to /odoo/backups/ on your remote server.')
    sftp_host = fields.Char('IP Address SFTP Server',
                            help='The IP address from your remote server. For example 192.168.0.1')
    sftp_port = fields.Integer('SFTP Port', help='The port on the FTP server that accepts SSH/SFTP calls.', default=22)
    sftp_user = fields.Char('Username SFTP Server',
                            help='The username where the SFTP connection should be made with. This is the user on the '
                                 'external server.')
    sftp_password = fields.Char('Password User SFTP Server',
                                help='The password from the user where the SFTP connection should be made with. This '
                                     'is the password from the user on the external server.')
    days_to_keep_sftp = fields.Integer('Remove SFTP after x days',
                                       help='Choose after how many days the backup should be deleted from the FTP '
                                            'server. For example:\nIf you fill in 5 the backups will be removed after '
                                            '5 days from the FTP server.',
                                       default=30)
    send_mail_sftp_fail = fields.Boolean('Auto. E-mail on backup fail',
                                         help='If you check this option you can choose to automaticly get e-mailed '
                                              'when the backup to the external server failed.')
    email_to_notify = fields.Char('E-mail to notify',
                                  help='Fill in the e-mail where you want to be notified that the backup failed on '
                                       'the FTP.')

    def test_sftp_connection(self, context=None):
        self.ensure_one()

        # Check if there is a success or fail and write messages
        message_title = ""
        message_content = ""
        error = ""
        has_failed = False

        for rec in self:
            ip_host = rec.sftp_host
            port_host = rec.sftp_port
            username_login = rec.sftp_user
            password_login = rec.sftp_password

            # Connect with external server over SFTP, so we know sure that everything works.
            try:
                s = paramiko.SSHClient()
                s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                s.connect(ip_host, port_host, username_login, password_login, timeout=10)
                sftp = s.open_sftp()
                sftp.close()
                message_title = _("Connection Test Succeeded!\nEverything seems properly set up for FTP back-ups!")
            except Exception as e:
                _logger.critical('There was a problem connecting to the remote ftp: %s', str(e))
                error += str(e)
                has_failed = True
                message_title = _("Connection Test Failed!")
                if len(rec.sftp_host) < 8:
                    message_content += "\nYour IP address seems to be too short.\n"
                message_content += _("Here is what we got instead:\n")
            finally:
                if s:
                    s.close()

        if has_failed:
            raise UserError(message_title + '\n\n' + message_content + "%s" % str(error))
        else:
            raise UserError(message_title + '\n\n' + message_content)

    @api.model
    def schedule_backup(self):
        conf_ids = self.search([])
        for rec in conf_ids:
            _logger.info('Starting database backup for database %s', rec.name)
            try:
                # 确保备份目录存在
                if not os.path.isdir(rec.folder):
                    try:
                        _logger.info('Creating backup directory %s', rec.folder)
                        os.makedirs(rec.folder, exist_ok=True)
                    except Exception as e:
                        _logger.error('Failed to create backup directory %s: %s', rec.folder, str(e))
                        continue

                # 创建备份文件名
                bkp_file = '%s_%s.%s' % (time.strftime('%Y_%m_%d_%H_%M_%S'), rec.name, rec.backup_type)
                file_path = os.path.join(rec.folder, bkp_file)
                _logger.info('Backing up database %s to %s', rec.name, file_path)

                try:
                    # 执行备份
                    self._take_dump(rec.name, file_path, backup_format=rec.backup_type)
                    
                    # 检查备份文件
                    if not os.path.exists(file_path):
                        raise Exception('Backup file was not created')
                    
                    file_size = os.path.getsize(file_path)
                    if file_size == 0:
                        raise Exception('Backup file has zero size')
                    
                    _logger.info('Successfully created backup file %s (size: %s bytes)', 
                               file_path, file_size)

                except Exception as error:
                    _logger.error('Failed to backup database %s: %s', rec.name, str(error))
                    if os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                            _logger.info('Removed failed backup file: %s', file_path)
                        except Exception as e:
                            _logger.error('Failed to remove failed backup file: %s', str(e))
                    continue

                # Check if user wants to write to SFTP or not.
                if rec.sftp_write:
                    _logger.info('Starting SFTP transfer for backup %s', file_path)
                    try:
                        # Store all values in variables
                        dir = rec.folder
                        path_to_write_to = rec.sftp_path
                        ip_host = rec.sftp_host
                        port_host = rec.sftp_port
                        username_login = rec.sftp_user
                        password_login = rec.sftp_password

                        try:
                            s = paramiko.SSHClient()
                            s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                            s.connect(ip_host, port_host, username_login, password_login, timeout=20)
                            sftp = s.open_sftp()
                        except Exception as error:
                            _logger.error('Error connecting to remote server! Error: %s', str(error))
                            raise

                        try:
                            sftp.chdir(path_to_write_to)
                        except IOError:
                            # Create directory and subdirs if they do not exist.
                            current_directory = ''
                            for dirElement in path_to_write_to.split('/'):
                                current_directory += dirElement + '/'
                                try:
                                    sftp.chdir(current_directory)
                                except:
                                    _logger.info('Creating remote directory %s', current_directory)
                                    sftp.mkdir(current_directory, 777)
                                    sftp.chdir(current_directory)

                        # Loop over all files in the directory.
                        for f in os.listdir(dir):
                            if rec.name in f:
                                fullpath = os.path.join(dir, f)
                                if os.path.isfile(fullpath):
                                    try:
                                        sftp.stat(os.path.join(path_to_write_to, f))
                                        _logger.debug('File %s already exists on remote server', fullpath)
                                    except IOError:
                                        try:
                                            sftp.put(fullpath, os.path.join(path_to_write_to, f))
                                            _logger.info('Successfully transferred %s to remote server', fullpath)
                                        except Exception as err:
                                            _logger.error('Failed to transfer file to remote server: %s', str(err))
                                            raise

                        # Remove old SFTP backups
                        _logger.info('Checking for old backups on SFTP server')
                        for file in sftp.listdir(path_to_write_to):
                            if rec.name in file:
                                fullpath = os.path.join(path_to_write_to, file)
                                timestamp = sftp.stat(fullpath).st_mtime
                                createtime = datetime.datetime.fromtimestamp(timestamp)
                                now = datetime.datetime.now()
                                delta = now - createtime
                                if delta.days >= rec.days_to_keep_sftp:
                                    if ".dump" in file or '.zip' in file:
                                        _logger.info('Removing old backup %s from SFTP server', file)
                                        sftp.unlink(file)

                        sftp.close()
                        s.close()
                    except Exception as e:
                        _logger.error('SFTP backup failed: %s', str(e))
                        if rec.send_mail_sftp_fail:
                            try:
                                self._send_sftp_fail_mail(rec, str(e))
                            except Exception as mail_error:
                                _logger.error('Failed to send error email: %s', str(mail_error))

                # Remove local backups
                if rec.autoremove:
                    _logger.info('Checking for old local backups to remove')
                    directory = rec.folder
                    for f in os.listdir(directory):
                        fullpath = os.path.join(directory, f)
                        if rec.name in fullpath:
                            timestamp = os.stat(fullpath).st_ctime
                            createtime = datetime.datetime.fromtimestamp(timestamp)
                            now = datetime.datetime.now()
                            delta = now - createtime
                            if delta.days >= rec.days_to_keep:
                                if os.path.isfile(fullpath) and (".dump" in f or '.zip' in f):
                                    _logger.info('Removing old local backup %s', fullpath)
                                    os.remove(fullpath)

            except Exception as e:
                _logger.error('Backup failed for database %s: %s', rec.name, str(e))
                continue

    def _send_sftp_fail_mail(self, rec, error):
        """发送SFTP失败通知邮件"""
        try:
            ir_mail_server = self.env['ir.mail_server'].search([], order='sequence asc', limit=1)
            message = f"""
            Dear Administrator,

            The backup for the server {rec.host} (IP: {rec.sftp_host}) failed.
            Please check the following details:

            IP address SFTP server: {rec.sftp_host}
            Username: {rec.sftp_user}
            Error details: {error}

            Best regards,
            Odoo Backup System
            """
            catch_all_domain = self.env["ir.config_parameter"].sudo().get_param("mail.catchall.domain")
            response_mail = f"auto_backup@{catch_all_domain}" if catch_all_domain else self.env.user.partner_id.email
            msg = ir_mail_server.build_email(
                response_mail,
                [rec.email_to_notify],
                f"Backup failed for {rec.host} ({rec.sftp_host})",
                message
            )
            ir_mail_server.send_email(msg)
        except Exception as e:
            _logger.error('Failed to send error notification email: %s', str(e))

    def _take_dump(self, db_name, backup_path, backup_format='zip'):
        """Backup database.
        :param db_name: name of the database
        :param backup_path: path of the backup file
        :param backup_format: The backup format (zip or dump)
        """
        _logger.info('Starting database dump for %s to %s', db_name, backup_path)

        try:
            _logger.debug('Connecting to database %s', db_name)
            db = odoo.sql_db.db_connect(str(db_name))
            with db.cursor() as cr:
                # 检查数据库是否存在
                cr.execute("SELECT 1 FROM pg_database WHERE datname = %s", (str(db_name),))
                if not cr.fetchone():
                    raise Exception("Database %s does not exist" % db_name)

            if backup_format == 'zip':
                with tempfile.TemporaryDirectory() as dump_dir:
                    _logger.debug('Created temporary directory: %s', dump_dir)

                    filestore = odoo.tools.config.filestore(str(db_name))
                    if os.path.exists(filestore):
                        _logger.debug('Copying filestore from %s', filestore)
                        shutil.copytree(str(filestore), os.path.join(str(dump_dir), 'filestore'))

                    manifest_path = os.path.join(str(dump_dir), 'manifest.json')
                    _logger.debug('Creating manifest file: %s', manifest_path)
                    with open(manifest_path, 'w') as fh:
                        with db.cursor() as cr:
                            json.dump(self._dump_db_manifest(cr), fh, indent=4)

                    dump_path = os.path.join(str(dump_dir), 'dump.sql')
                    _logger.debug('Dumping database to: %s', dump_path)
                    cmd = [find_pg_tool('pg_dump'), '--no-owner', '--file=' + str(dump_path), str(db_name)]
                    _logger.debug('Running command: %s', ' '.join(cmd))
                    env = exec_pg_environ()
                    _logger.debug('Using environment: %s', env)
                    subprocess.run(cmd, env=env, check=True)

                    _logger.debug('Creating zip file at: %s', backup_path)
                    osutil.zip_dir(
                        dump_dir,
                        str(backup_path),
                        include_dir=False,
                        fnct_sort=lambda file_name: file_name != 'dump.sql',
                    )

            else:
                _logger.debug('Creating custom format backup at: %s', backup_path)
                cmd = [find_pg_tool('pg_dump'), '--no-owner', '--format=c', '--file=' + str(backup_path), str(db_name)]
                _logger.debug('Running command: %s', ' '.join(cmd))
                env = exec_pg_environ()
                _logger.debug('Using environment: %s', env)
                subprocess.run(cmd, env=env, check=True)

            if not os.path.exists(str(backup_path)):
                raise Exception("Backup file was not created")

            backup_size = os.path.getsize(str(backup_path))
            if backup_size == 0:
                raise Exception("Backup file has zero size")

            _logger.info('Successfully created backup %s (size: %s bytes)', backup_path, backup_size)
            return True

        except Exception as e:
            _logger.error('Database dump failed: %s', str(e))
            if os.path.exists(str(backup_path)):
                try:
                    os.unlink(str(backup_path))
                    _logger.info('Removed failed backup file: %s', backup_path)
                except OSError as e:
                    _logger.error('Could not remove failed backup file: %s', str(e))
            raise

    def _dump_db_manifest(self, cr):
        pg_version = "%d.%d" % divmod(cr._obj.connection.server_version / 100, 100)
        cr.execute("SELECT name, latest_version FROM ir_module_module WHERE state = 'installed'")
        modules = dict(cr.fetchall())
        manifest = {
            'odoo_dump': '1',
            'db_name': cr.dbname,
            'version': odoo.release.version,
            'version_info': odoo.release.version_info,
            'major_version': odoo.release.major_version,
            'pg_version': pg_version,
            'modules': modules,
        }
        return manifest
