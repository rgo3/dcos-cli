/*
 * DC/OS
 *
 * DC/OS API
 *
 * API version: 1.0.0
 */

// Code generated by OpenAPI Generator (https://openapi-generator.tech); DO NOT EDIT.

package dcos

type EdgelbV2ServiceMarathon struct {
	// Marathon pod or application ID.
	ServiceID        string `json:"serviceID,omitempty"`
	ServiceIDPattern string `json:"serviceIDPattern,omitempty"`
	// Marathon pod container name, optional unless using Marathon pods.
	ContainerName        string `json:"containerName,omitempty"`
	ContainerNamePattern string `json:"containerNamePattern,omitempty"`
}