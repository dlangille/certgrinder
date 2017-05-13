import yaml, os, subprocess, tempfile, shutil, OpenSSL, logging, textwrap
from datetime import datetime
from pid import PidFile

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


class Certgrinder:
    def __init__(self):
        """
        The __init__ method just reads the config file and checks a few things
        """
        if not self.read_config():
            sys.exit(1)

        if 'domainlist' not in self.conf:
            logger.error("domainlist not found in conf")
            sys.exit(1)


    def read_config(self):
        """
        Actually reads and parses the yaml config file
        """
        with open("certgrinder.yml", "r") as f:
            try:
                self.conf = yaml.load(f)
                return True
            except Exception as E:
                logger.exception("Unable to read config")
                return False


    ############# RSA KEY METHODS ################################################


    def load_keypair(self):
        """
        Checks if the keypair file exists on disk, and calls self.create_keypair() if not
        """
        if os.path.exists(self.keypair_path):
            keypair_string=open(self.keypair_path, 'r').read()
            self.keypair=OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM, keypair_string)
        else:
            logger.debug("keypair %s not found, creating new keypair..." % self.keypair_path)
            self.create_keypair()

        return self.keypair


    def create_keypair(self):
        """
        Generates an RSA keypair
        """
        self.keypair = OpenSSL.crypto.PKey()
        self.keypair.generate_key(OpenSSL.crypto.TYPE_RSA, 4096)
        self.save_keypair()


    def save_keypair(self):
        """
        Saves RSA keypair to disk
        """
        with open(self.keypair_path, 'w') as f:
            f.write(OpenSSL.crypto.dump_privatekey(OpenSSL.crypto.FILETYPE_PEM, self.keypair))
        logger.debug("saved keypair to %s" % self.keypair_path)


############# CSR METHODS ################################################


    def generate_csr(self, domains):
        """
        Generates a new CSR
        """
        logger.info("Generating CSR for domains: %s" % domains)
        self.csr = OpenSSL.crypto.X509Req()

        # set public key 
        self.csr.set_pubkey(self.keypair)

        # set all the x509 properties from the config
        for key, value in self.conf['x509'].items():
            setattr(self.csr.get_subject(), key, value)

        # set CN (this can be removed when everyone supports rfc6125 which
        # says that CN should not be checked when subjectAltName is present)
        setattr(self.csr.get_subject(), 'CN', domains[0])

        # add subjectAltName x598 extension
        altnames = ', '.join(['DNS:%s' % domain for domain in domains])
        logger.debug("Adding subjectAltName extension with value %s" % altnames)
        self.csr.add_extensions([
            OpenSSL.crypto.X509Extension(
                type_name="subjectAltName",
                critical=False, # TODO: should this be critical=True?!
                value=altnames
            )
        ])

        # sign the CSR
        self.csr.sign(self.keypair, 'sha256')

        # write the csr to disk
        self.save_csr()
        return self.csr


    def save_csr(self):
        """
        Save the PEM version of the CSR to the path in self.csr_path
        """
        with open(self.csr_path, 'w') as f:
            f.write(OpenSSL.crypto.dump_certificate_request(OpenSSL.crypto.FILETYPE_PEM, self.csr))
        logger.debug("saved CSR to %s" % self.csr_path)



############# CERTIFICATE METHODS ################################################


    def load_certificate(self):
        if os.path.exists(self.certificate_path):
            certificate_string=open(self.certificate_path, 'r').read()
            self.certificate=OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, certificate_string)
        else:
            logger.debug("certificate %s not found" % self.keypair_path)
            self.certificate = False

        return self.certificate


    def check_certificate_validity(self):
        """
        Checks the validity of the certificate.
        Returns a simpe True or False based on self.conf['cert_renew_threshold_days']
        """
        notafter = self.certificate.get_notAfter()
        try:
            expiration = datetime.strptime(notafter, "%Y%m%d%H%M%SZ")
        except Exception as E:
            logger.exception("Got exception while parsing notAfter from x509: %s" % notafter)
            return False
        # find the timedelta between now and the expire_date
        expiredelta = expiration - datetime.now()

        if expiredelta.days() < self.conf['cert_renew_threshold_days']:
            # we are close to expiry
            return False
        else:
            # not yet
            return True

    def get_new_certificate(self):
        """
        cat the csr over ssh to the certgrinder server.
        """
        logger.info("ready to get signed certificate using csr %s" % self.csr_path)
        p = subprocess.Popen(['ssh', self.conf['server'], self.conf['csrgrinder_path']], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = p.communicate(input=OpenSSL.crypto.dump_certificate_request(OpenSSL.crypto.FILETYPE_PEM, self.csr))

        # parse the certificate in stdout (which should contains a valid PEM certificate)
        try:
            self.certificate = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, stdout)
        except Exception as E:
            logger.exception("There was a problem getting the new certificate. This was stdout:")
            logger.error(stdout)
            logger.error("this was in stderr:")
            logger.error(stderr)
            return False

        # save cert to disk
        self.save_certificate()
        return True


    def save_certificate(self):
        """
        Save the PEM certificate to the path self.certificate_path
        """
        with open(self.certificate_path, 'w') as f:
            f.write(OpenSSL.crypto.dump_certificate(OpenSSL.crypto.FILETYPE_PEM, self.certificate))
        logger.info("saved new certificate to %s" % self.certificate_path)


############# MAIN METHOD ################################################


    def grind(self, domains):
        # set paths
        self.keypair_path = os.path.join(self.conf['path'], '%s.key' % domains[0])
        logger.debug("key path: %s" % self.keypair_path)

        self.certificate_path = os.path.join(self.conf['path'], '%s.crt' % domains[0])
        logger.debug("cert path: %s" % self.certificate_path)

        self.csr_path = os.path.join(self.conf['path'], '%s.csr' % domains[0])
        logger.debug("csr path: %s" % self.csr_path)

        # attempt to load/generate keypair for this set of domains
        if self.load_keypair():
            logger.debug("Loaded key %s" % self.keypair_path)
        else:
            logger.error("Unable to load or generate keypair %s" % self.keypair_path)
            return False

        # attempt to load certificate (if we even have one)
        if self.load_certificate():
            logger.debug("Loaded certificate %s, checking validity..." % self.certificate_path)
            if self.check_certificate_validity():
                logger.debug("The certificate %s is valid for at least another %s days, skipping" % (self.certificate_path, days))
                return True
            else:
                logger.info("The certificate %s is valid for less than %s days, renewing..." % (self.certificate_path, self.conf['cert_renew_threshold_days']))
        else:
            logger.debug("Unable to load certificate %s" % self.certificate_path)

        # generate new CSR
        logger.info("Generating new CSR for domains %s" % domains)
        if self.generate_csr(domains):
            logger.info("Generated new CSR, getting certificate...")
        else:
            logger.error("Unable to generate new CSR for domains: %s" % domains)
            return False

        # use CSR to get signed certificate
        if self.get_new_certificate():
            logger.info("Successfully got new certificate for domains: %s" % domains)
            return True
        else:
            logger.error("Unable to get certificate for domains: %s" % domains)
            return False


if __name__ == '__main__':
        """
        Main method. Simply loops over sets of domains in the config and
        calls certgrinder.grind() for each
        """
        certgrinder = Certgrinder()

        with PidFile(piddir=certgrinder.conf['path']):
            for domains in certgrinder.conf['domainlist']:
                logger.info("------ processing domains: %s" % domains)
                if certgrinder.grind(domains):
                    logger.info("----- successfully processed domains: %s" % domains)
                else:
                    logger.error("----- error processing domains: %s" % domains)

            logger.debug("Done")


