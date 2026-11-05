ARG JENKINS_BASE=jenkins/jenkins:2.580.2-jdk25
FROM ${JENKINS_BASE}
COPY .cache/plugins/ /usr/share/jenkins/ref/plugins/
COPY jenkins/ /opt/xran/
COPY jenkins/init.groovy /usr/share/jenkins/ref/init.groovy.d/20-xran.groovy.override
COPY jenkins/parameters.js /usr/share/jenkins/ref/userContent/xran-parameters.js.override
