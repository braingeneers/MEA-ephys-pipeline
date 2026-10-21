// Hash only staged executable source, never the scientific payload directories.
// Local/ConfigMap launches do not have Nextflow's Git-asset content hashing.
def meaCodeDigest(List inputs) {
    def digest = java.security.MessageDigest.getInstance('SHA-256')
    digest.update('mea.staged-code/v1\u0000'.getBytes('UTF-8'))
    def visit
    visit = { java.nio.file.Path path, String relative, Set ancestors ->
        def real = path.toRealPath()
        if (java.nio.file.Files.isDirectory(real)) {
            if (ancestors.contains(real)) throw new IllegalArgumentException("Cyclic staged code directory: ${path}")
            digest.update(('D\u0000' + relative + '\u0000').getBytes('UTF-8'))
            def children = []
            def stream = java.nio.file.Files.newDirectoryStream(real)
            try { stream.each { children.add(it) } }
            finally { stream.close() }
            children.sort { a, b -> a.fileName.toString() <=> b.fileName.toString() }
                .each { child -> visit(child, relative + '/' + child.fileName.toString(), ancestors + [real]) }
        } else if (java.nio.file.Files.isRegularFile(real)) {
            def size = java.nio.file.Files.size(real)
            digest.update(('F\u0000' + relative + '\u0000' + size + '\u0000').getBytes('UTF-8'))
            def stream = java.nio.file.Files.newInputStream(real)
            try {
                byte[] buffer = new byte[65536]
                int count
                while ((count = stream.read(buffer)) != -1) digest.update(buffer, 0, count)
            } finally { stream.close() }
        } else {
            throw new IllegalArgumentException("Staged code is not a regular file or directory: ${path}")
        }
    }
    inputs.eachWithIndex { path, index -> visit(path.toRealPath(), index.toString(), [] as Set) }
    digest.digest().encodeHex().toString()
}

// A code port carries an explicit value as well as its staged path. Nextflow
// hashes this value on both supported engines, including Gitless local launches.
def meaCodeInput(path) {
    tuple(meaCodeDigest([path]), path)
}
