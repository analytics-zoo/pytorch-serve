package org.pytorch.serve.archive.model;

import javax.crypto.Cipher;
import javax.crypto.CipherInputStream;
import javax.crypto.spec.SecretKeySpec;
import javax.crypto.NoSuchPaddingException;
import java.security.InvalidKeyException;
import java.security.NoSuchAlgorithmException;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.io.IOException;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.util.List;
import org.apache.commons.io.FileUtils;
import org.apache.commons.io.FilenameUtils;
import org.pytorch.serve.archive.DownloadArchiveException;
import org.pytorch.serve.archive.utils.ArchiveUtils;
import org.pytorch.serve.archive.utils.InvalidArchiveURLException;
import org.pytorch.serve.archive.utils.ZipUtils;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class ModelArchive {

    private static final Logger logger = LoggerFactory.getLogger(ModelArchive.class);

    private static final String MANIFEST_FILE = "MANIFEST.json";

    private Manifest manifest;
    private String url;
    private File modelDir;
    private boolean extracted;

    public ModelArchive(Manifest manifest, String url, File modelDir, boolean extracted) {
        this.manifest = manifest;
        this.url = url;
        this.modelDir = modelDir;
        this.extracted = extracted;
    }

    public static ModelArchive downloadModel(
            List<String> allowedUrls, String modelStore, String url)
            throws ModelException, FileAlreadyExistsException, IOException,
                    DownloadArchiveException {
        return downloadModel(allowedUrls, modelStore, false, null, url, false);
    }

    public static CipherInputStream decryptFile(InputStream inputStream, String keyStore)
            throws NoSuchAlgorithmException, NoSuchPaddingException, InvalidKeyException, IOException {
            try {
                byte[] key = Files.readAllBytes(Path.of(keyStore));

                Cipher cipher = Cipher.getInstance("AES/ECB/PKCS5Padding");
                SecretKeySpec secretKeySpec = new SecretKeySpec(key, "AES");
                cipher.init(Cipher.DECRYPT_MODE, secretKeySpec);

                CipherInputStream cipherInputStream = new CipherInputStream(inputStream, cipher);
                return cipherInputStream;
            } catch (NoSuchAlgorithmException | NoSuchPaddingException | InvalidKeyException | IOException e) {
                e.printStackTrace();
                throw e;
            }
    }

    public static ModelArchive downloadModel(
            List<String> allowedUrls, String modelStore, boolean isModelEncryption, String keyStore, String url, boolean s3SseKmsEnabled)
            throws ModelException, FileAlreadyExistsException, IOException,
                    DownloadArchiveException {
        if (modelStore == null) {
            throw new ModelNotFoundException("Model store has not been configured.");
        }

        if (url == null || url.isEmpty()) {
            throw new ModelNotFoundException("empty url");
        }

        String marFileName = FilenameUtils.getName(url);
        File modelLocation = new File(modelStore, marFileName);
        try {
            ArchiveUtils.downloadArchive(
                    allowedUrls, modelLocation, marFileName, url, s3SseKmsEnabled);
        } catch (InvalidArchiveURLException e) {
            throw new ModelNotFoundException(e.getMessage()); // NOPMD
        }

        if (url.contains("..")) {
            throw new ModelNotFoundException("Relative path is not allowed in url: " + url);
        }

        if (modelLocation.isFile()) {
            try (InputStream is = Files.newInputStream(modelLocation.toPath())) {
                try {
                    if (isModelEncryption && keyStore != null) {
			CipherInputStream cis =  decryptFile(is, keyStore);
                        InputStream unzipStream = ZipUtils.getManifest(cis);
			return load(url, unzipStream, true, modelStore);
                    }
                    else {
			File unzipDir;
                        unzipDir = ZipUtils.unzip(is, null, "models");
                        return load(url, unzipDir, true);
		    }
                } catch (NoSuchAlgorithmException | NoSuchPaddingException | InvalidKeyException | IOException e) {
                    throw new ModelNotFoundException("Model not found at: " + url);
                }
            }
        }

        if (new File(url).isDirectory()) {
            // handle the case that the input url is a directory.
            // the input of url is "/xxx/model_store/modelXXX" or
            // "xxxx/yyyyy/modelXXX".
            return load(url, new File(url), false);
        } else if (modelLocation.exists()) {
            // handle the case that "/xxx/model_store/modelXXX" is directory.
            // the input of url is modelXXX when torchserve is started
            // with snapshot or with parameter --models modelXXX
            return load(url, modelLocation, false);
        }

        throw new ModelNotFoundException("Model not found at: " + url);
    }

    private static ModelArchive load(String url, File dir, boolean extracted)
            throws InvalidModelException, IOException {
        boolean failed = true;
        try {
            File manifestFile = new File(dir, "MAR-INF/" + MANIFEST_FILE);
            Manifest manifest = null;
            if (manifestFile.exists()) {
                manifest = ArchiveUtils.readFile(manifestFile, Manifest.class);
            } else {
                manifest = new Manifest();
            }

            failed = false;
            return new ModelArchive(manifest, url, dir, extracted);
        } finally {
            if (extracted && failed) {
                FileUtils.deleteQuietly(dir);
            }
        }
    }

    private static ModelArchive load(String url, InputStream is, boolean extracted, String dir)
            throws InvalidModelException, IOException {
        boolean failed = true;
        try {
            Manifest manifest = null;
            if (is != null) {
                manifest = ArchiveUtils.readInputStream(is, Manifest.class);
            } else {
                manifest = new Manifest();
            }

            failed = false;
            return new ModelArchive(manifest, url, new File(dir), extracted);
        } finally {
            if (extracted && failed) {
                //FileUtils.deleteQuietly(dir);
            }
        }
    }


    public void validate() throws InvalidModelException {
        Manifest.Model model = manifest.getModel();
        try {
            if (model == null) {
                throw new InvalidModelException("Missing Model entry in manifest file.");
            }

            if (model.getModelName() == null) {
                throw new InvalidModelException("Model name is not defined.");
            }

            if (model.getModelVersion() == null) {
                throw new InvalidModelException("Model version is not defined.");
            }

            if (manifest.getRuntime() == null) {
                throw new InvalidModelException("Runtime is not defined or invalid.");
            }

            if (manifest.getArchiverVersion() == null) {
                logger.warn(
                        "Model archive version is not defined. Please upgrade to torch-model-archiver 0.2.0 or higher");
            }

            if (manifest.getCreatedOn() == null) {
                logger.warn(
                        "Model archive createdOn is not defined. Please upgrade to torch-model-archiver 0.2.0 or higher");
            }
        } catch (InvalidModelException e) {
            clean();
            throw e;
        }
    }

    public static void removeModel(String modelStore, String marURL) {
        if (ArchiveUtils.isValidURL(marURL)) {
            String marFileName = FilenameUtils.getName(marURL);
            File modelLocation = new File(modelStore, marFileName);
            FileUtils.deleteQuietly(modelLocation);
        }
    }

    public String getHandler() {
        return manifest.getModel().getHandler();
    }

    public Manifest getManifest() {
        return manifest;
    }

    public String getUrl() {
        return url;
    }

    public File getModelDir() {
        return modelDir;
    }

    public String getModelName() {
        return manifest.getModel().getModelName();
    }

    public String getModelVersion() {
        return manifest.getModel().getModelVersion();
    }

    public void clean() {
        if (url != null && extracted) {
            FileUtils.deleteQuietly(modelDir);
        }
    }
}
